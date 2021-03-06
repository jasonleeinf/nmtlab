#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import sys
import os
import time
from collections import defaultdict
from six.moves import xrange
from abc import abstractmethod, ABCMeta

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.optim.optimizer import Optimizer
from torch.autograd import Variable
from torchtext.data.batch import Batch

from nmtlab.models import EncoderDecoderModel
from nmtlab.utils import smoothed_bleu
from nmtlab.dataset import MTDataset
from nmtlab.schedulers import Scheduler
from nmtlab.utils import OPTS

import higher

ROOT_RANK = 0

class TrainerKit(object):
    """Training NMT models.
    """

    __metaclass__ = ABCMeta

    def __init__(self, model, dataset, optimizers, scheduler=None, multigpu=False, using_horovod=True):
        """Create a trainer.
        Args:
            model (EncoderDecoderModel): The model to train.
            dataset (MTDataset): Bilingual dataset.
            scheduler (Scheduler): Training scheduler.
        """
        self.enc_param_names = ["embed_layer", "x_encoder", "q_encoder", "q_hid2lat"]
        self.kl_grad, self.nll_grad, self.total_grad, self.param_norm = {}, {}, {}, {}
        self._model = model
        self._dataset = dataset
        self.inner_optimizer = optimizers[0]
        self.outer_optimizer = optimizers[1]
        self._optimizer  = self.outer_optimizer
        self._scheduler = scheduler if scheduler is not None else Scheduler()
        self._multigpu = multigpu
        self._horovod = using_horovod
        self._n_devices = 1
        self._cuda_avaiable = torch.cuda.is_available()
        # Setup horovod1i
        self.register_model(model)
        self._n_devices = self.device_count()
        # Initialize common variables
        self._log_lines = []
        self._scheduler.bind(self)
        self._best_criteria = 65535
        self._n_train_batch = self._dataset.n_train_batch()
        self._batch_size = self._dataset.batch_size()
        self.configure()
        self._begin_time = 0
        self._current_epoch = 0
        self._current_step = 0
        self._global_step = 0
        self._train_scores = defaultdict(float)
        self._train_count = 0
        self._checkpoint_count = 0
        self._train_writer = None
        self._dev_writer = None
        self._tensorboard_namespace = None
        # Print information
        self.log("nmtlab", "Training {} with {} parameters".format(
            self._model.__class__.__name__, len(list(self._model.named_parameters()))
        ))
        self.log("nmtlab", "with {} and {}".format(
            self._optimizer.__class__.__name__, self._scheduler.__class__.__name__
        ))
        self.log("nmtlab", "Training data has {} batches".format(self._dataset.n_train_batch()))
        self._report_valid_data_hash()
        device_name = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU"
        self.log("nmtlab", "Running with {} GPUs ({})".format(
            self.device_count(), device_name
        ))

    def device_count(self):
        if self._multigpu:
            if self.using_horovod():
                import horovod.torch as hvd
                return hvd.size()
            else:
                return torch.cuda.device_count()
        else:
            return 1

    def register_model(self, model=None):
        """Register a model and send it to gpu(s).
        """
        if model is None:
            model = self._model
        if self._multigpu and self._horovod:
            # Horovod based
            try:
                import horovod.torch as hvd
            except ImportError:
                raise SystemError("horovod is not working, try to set using_horovod=False.")
            from nmtlab.trainers.distributed_optim import FlexibleDistributedOptimizer
            # Initialize Horovod
            hvd.init()
            # Pin GPU to be used to process local rank (one GPU per process)
            torch.cuda.set_device(hvd.local_rank())
            self._model = model
            self._model.cuda()
            self._optimizer = FlexibleDistributedOptimizer(self._optimizer, named_parameters=self._model.named_parameters())
            hvd.broadcast_parameters(self._model.state_dict(), root_rank=ROOT_RANK)
            # Set the scope of training data
            self._dataset.set_gpu_scope(hvd.rank(), hvd.size())
        elif self._multigpu:
            # Pytorch-based multi gpu backend
            model.cuda()
            self._model = nn.DataParallel(model)
        elif torch.cuda.is_available():
            # Single-gpu case
            self._model = model
            self._model.cuda()
        else:
            self._model = model

    def configure(self, save_path=None, clip_norm=0, matchnorm_lr=0.0,
                  scale_klgrad_iter=-1, match_gradnorm_iter=-1, kl_annealing=False,
                  scale_klgrad_only_smaller=False, minimize_cosine_iter=-1,
                  n_valid_per_epoch=10, criteria="loss",
                  comp_fn=min, checkpoint_average=0,
                  tensorboard_logdir=None, tensorboard_namespace=None):
        """Configure the hyperparameters of the trainer.
        """
        self._save_path = save_path
        self._clip_norm = clip_norm
        self._matchnorm_lr = matchnorm_lr
        self._minimize_cosine_iter = minimize_cosine_iter
        self._scale_klgrad_iter = scale_klgrad_iter
        self._scale_klgrad_only_smaller = scale_klgrad_only_smaller
        self._match_gradnorm_iter = match_gradnorm_iter
        self._n_valid_per_epoch = n_valid_per_epoch
        self.kl_annealing = kl_annealing
        self._criteria = criteria
        self._comp_fn = comp_fn
        assert self._comp_fn in (min, max)
        if self._comp_fn is max:
            self._best_criteria = -10000.0
        self._checkpoint_average = checkpoint_average
        # assert self._criteria in ("bleu", "loss", "mix")
        self._valid_freq = int(self._n_train_batch / self._n_valid_per_epoch)
        if tensorboard_logdir is not None and self._is_root_node():
            try:
                from tensorboardX import SummaryWriter
                if tensorboard_namespace is None:
                    tensorboard_namespace = "nmtlab"
                tensorboard_namespace = tensorboard_namespace.replace(".", "_")
                self._train_writer = SummaryWriter(
                    log_dir=tensorboard_logdir+"_train", comment=tensorboard_namespace, flush_secs=20)
                self._dev_writer = SummaryWriter(
                    log_dir=tensorboard_logdir+"_dev", comment=tensorboard_namespace, flush_secs=20)
                self._tensorboard_namespace = tensorboard_namespace
            except ModuleNotFoundError:
                print("[trainer] tensorboardX is not found, logger is disabled.")

    @abstractmethod
    def run(self):
        """Run the training from begining to end.
        """

    def extract_vars(self, batch):
        """Extract variables from batch
        """
        if isinstance(self._dataset, MTDataset):
            src_seq = Variable(batch.src.transpose(0, 1))
            tgt_seq = Variable(batch.tgt.transpose(0, 1))
            vars = [src_seq, tgt_seq]
        else:
            vars = []
            if isinstance(batch, Batch):
                batch_vars = list(batch)[0]
            else:
                batch_vars = batch
            for x in batch_vars:
                if type(x) == np.array:
                    if "int" in str(x.dtype):
                        x = x.astype("int64")
                    x = Variable(torch.tensor(x))
                vars.append(x)
        if self._cuda_avaiable:
            vars = [var.cuda() if isinstance(var, torch.Tensor) else var for var in vars]
        return vars

    def get_grads(self):
        dot_product = torch.sum(
            torch.stack([(kl * nll).sum() for kl, nll in zip(
                self.kl_grad.values(), self.nll_grad.values())
            ])
        )

        kl = torch.sum(
            torch.stack([(ep ** 2).sum() for ep in self.kl_grad.values()])
        ).sqrt()

        nll = torch.sum(
            torch.stack([(ep ** 2).sum() for ep in self.nll_grad.values()])
        ).sqrt()

        cosine = dot_product / (kl * nll)

        total = torch.sum(
            torch.stack([(ep ** 2).sum() for ep in self.total_grad.values()])
        ).sqrt()

        param = torch.sum(
            torch.stack([(ep ** 2).sum() for ep in self.param_norm.values()])
        ).sqrt()

        return kl.item(), nll.item(), total.item(), param.item(), cosine.item()

    def train(self, batch):
        vars = self.extract_vars(batch)
        inner_lr = self.inner_optimizer.param_groups[0]["lr"]

        if (self._global_step < self._match_gradnorm_iter) or (self._global_step < self._minimize_cosine_iter):
            with higher.innerloop_ctx(self._model, self.inner_optimizer) as (fmodel, diffopt):
                if self._current_epoch % 2 == 0:
                    inner_vars = [vars[0][::2], vars[1][::2]]
                else:
                    inner_vars = [vars[0][1::2], vars[1][1::2]]

                val_map = fmodel(*inner_vars)
                diffopt.step(val_map["nll"])
                val_map = fmodel(*inner_vars)
                diffopt.step(val_map["kl"])

                norm_grad_diff = 0.0
                dot, norm1, norm2 = 0.0, 0.0, 0.0
                nll_grad_sum, kl_grad_sum = 0.0, 0.0
                for p0, p1, p2 in zip(
                    fmodel._fast_params[0],
                    fmodel._fast_params[1],
                    fmodel._fast_params[2],
                ):
                    nll_grad = (p1 - p0) / inner_lr
                    kl_grad = (p2 - p1) / inner_lr
                    if self._global_step < self._match_gradnorm_iter:
                        if kl_grad.sum().item() != 0:
                            nll_grad_sum += (nll_grad ** 2).sum()
                            kl_grad_sum += (kl_grad ** 2).sum()
                    else:
                        dot += (nll_grad * kl_grad).sum()
                        norm1 += (nll_grad ** 2).sum()
                        norm2 += (kl_grad ** 2).sum()

                norm_grad_diff = (nll_grad_sum - kl_grad_sum) ** 2

                if self._global_step < self._match_gradnorm_iter:
                    norm_grad_diff = norm_grad_diff.sqrt()
                else:
                    norm_grad_diff = dot / ( norm1.sqrt() * norm2.sqrt() )

                grad_of_grads = torch.autograd.grad(norm_grad_diff, fmodel.init_fast_params, allow_unused=True)
                if self._clip_norm > 0:
                    real_grad_of_grads = [g for g in grad_of_grads if not g is None]
                    total_norm = torch.norm(
                        torch.stack([torch.norm(g.detach()) for g in real_grad_of_grads]))
                    clip_coef = self._clip_norm / (total_norm + 1e-6)
                    if clip_coef > 1:
                        clip_coef = 1

        self.outer_optimizer.zero_grad()
        val_map = self._model(*vars)

        # case 1) matching norm of the gradient differences
        #   monitor the gradients here
        # case 2) scaling norm of KL gradient, so that it's at most the norm of the NLL gradient
        #   monitor gradients and compute gradients to rescale
        # case 3) standzrd ELBO training
        #   monitor the gradients
        # case 4) KL annealing
        #   monitor the gradients
        if (self._global_step % 100 == 0) or (self._global_step < self._scale_klgrad_iter):
            self.kl_grad, self.nll_grad, self.total_grad, self.param_norm = {}, {}, {}, {}
            val_map["nll"].backward(retain_graph=True)

            nll_norm, kl_norm = 0.0, 0.0
            for name, param in self._model.named_parameters():
                if any([name.startswith(xx) for xx in self.enc_param_names]):
                    self.param_norm[name] = param.data.clone()
                    self.nll_grad[name] = param.grad.data.clone()
                    nll_norm += (self.nll_grad[name] ** 2).sum()

            val_map["kl"].backward(retain_graph=False)
            for name, param in self._model.named_parameters():
                if any([name.startswith(xx) for xx in self.enc_param_names]):
                    self.total_grad[name] = param.grad.data.clone()
                    self.kl_grad[name] = param.grad.data.clone() - self.nll_grad[name]
                    kl_norm += (self.kl_grad[name] ** 2).sum()

            if self._global_step < self._scale_klgrad_iter:
                mul = nll_norm.sqrt() / kl_norm.sqrt()

                if kl_norm.sqrt() > nll_norm.sqrt() or (not self._scale_klgrad_only_smaller):
                    for name, param in self._model.named_parameters():
                        if any([name.startswith(xx) for xx in self.enc_param_names]):
                            param.grad.data.sub_(self.kl_grad[name] * (1.0 - mul))
                val_map["len_loss"].backward()
        else:
            val_map["loss"].backward()

        if self._global_step % 100 == 0:
            kl, nll, total, param, cosine = self.get_grads()
            self._train_writer.add_scalar(
                "{}/{}".format(self._tensorboard_namespace, "cosine"), cosine, self._global_step)
            self._train_writer.add_scalar(
                "{}/{}".format(self._tensorboard_namespace, "kl_grad_norm"), kl, self._global_step)
            self._train_writer.add_scalar(
                "{}/{}".format(self._tensorboard_namespace, "nll_grad_norm"), nll, self._global_step)
            self._train_writer.add_scalar(
                "{}/{}".format(self._tensorboard_namespace, "total_grad_norm"), total, self._global_step)
            self._train_writer.add_scalar(
                "{}/{}".format(self._tensorboard_namespace, "param_norm"), param, self._global_step)
            if self._global_step < self._match_gradnorm_iter:
                self._train_writer.add_scalar(
                    "{}/{}".format(self._tensorboard_namespace, "norm_grad_diff"), norm_grad_diff.item(), self._global_step)

        if self._global_step % 100 != 0:
            if self._clip_norm > 0:
                torch.nn.utils.clip_grad_norm_(self._model.parameters(), self._clip_norm)
            self.outer_optimizer.step()

        if (self._global_step < self._match_gradnorm_iter) or (self._global_step < self._minimize_cosine_iter):
            for grad, param in zip(grad_of_grads, self._model.parameters()):
                if not grad is None:
                    param.data.add_(grad, alpha=-1.0 * self._matchnorm_lr * clip_coef)
        self.print_progress(val_map)
        self.record_train_scores(val_map)
        self._global_step += 1
        if self._global_step % 100 == 0:
            for key, val in val_map.items():
                if self._train_writer is not None:
                    self._train_writer.add_scalar(
                        "{}/{}".format(self._tensorboard_namespace, key), val.item(), self._global_step)

        return val_map

    def valid(self, force=False):
        """Validate the model every few steps.
        """
        valid_condition = (self._current_step + 1) % self._valid_freq == 0 or force
        if valid_condition and self._is_root_node():
            self._model.train(False)
            score_map = self.run_valid()
            is_improved = self.check_improvement(score_map)
            self._scheduler.after_valid(is_improved, score_map)
            self._model.train(True)
            self.log("valid", "{}{} (epoch {}, step {})".format(
                self._dict_str(score_map), " *" if is_improved else "",
                self._current_epoch + 1, self._global_step + 1
            ))
        # Check new trainer settings when using horovod
        if valid_condition and self._multigpu and self._horovod:
            self.synchronize_learning_rate()
        if (self._current_step + 1) % 1000 == 0 and self._multigpu and self._horovod:
            import horovod.torch as hvd
            hvd.init()
            from nmtlab.trainers.hvd_utils import broadcast_optimizer_state
            import horovod.torch as hvd
            broadcast_optimizer_state(self._optimizer, ROOT_RANK)
            hvd.broadcast_parameters(self._model.state_dict(), ROOT_RANK)

    def run_valid(self):
        """Run the model on the validation set and report loss.
        """
        score_map = defaultdict(list)
        # print("enter run valid")
        for batch in self._dataset.valid_set():
            vars = self.extract_vars(batch)
            if self._model.enable_valid_grad:
                val_map = self._model(*vars, sampling=True)
                self._model.zero_grad()
            else:
                with torch.no_grad():
                    val_map = self._model(*vars, sampling=True)
            # Estimate BLEU
            if "sampled_tokens" in val_map and val_map["sampled_tokens"] is not None:
                tgt_seq = vars[1]
                bleu = self._compute_bleu(val_map["sampled_tokens"], tgt_seq)
                score_map["bleu"].append(- bleu)
                if self._criteria == "mix":
                    # Trade 1 bleu point for 0.02 decrease in loss
                    score_map["mix"].append(- bleu + val_map["loss"] / 0.02)
                del val_map["sampled_tokens"]
            for k, v in val_map.items():
                if v is not None:
                    score_map[k].append(v)
        for key, vals in score_map.items():
            if self._multigpu and not self._horovod:
                val = np.mean([v.mean().cpu() for v in vals])
            else:
                if "_mu" in key or "_sigma" in key:
                    val = torch.cat(vals).cpu()
                else:
                    val = np.mean([v.cpu().detach() for v in vals])
            score_map[key] = val
            if self._dev_writer is not None:
                if val.size == 1:
                    self._dev_writer.add_scalar("{}/{}".format(self._tensorboard_namespace, key), val, self._global_step)
                else:
                    self._dev_writer.add_histogram("{}/{}".format(self._tensorboard_namespace, key), val, self._global_step)
        for key in list(score_map.keys()):
            if "_mu" in key or "_sigma" in key:
                del score_map[key]
        return score_map

    def check_improvement(self, score_map):
        cri = score_map[self._criteria]
        self._checkpoint_count += 1
        if self._checkpoint_average > 0:
            self.save(path=self._save_path + ".chk{}".format(self._checkpoint_count))
            old_checkpoint = self._save_path + ".chk{}".format(self._checkpoint_count - self._checkpoint_average)
            if os.path.exists(old_checkpoint):
                os.remove(old_checkpoint)
        # if cri < self._best_criteria - abs(self._best_criteria) * 0.001:
        if self._comp_fn(cri, self._best_criteria) == cri:
            self._best_criteria = cri
            if self._checkpoint_average <= 0:
                self.save()
            return True
        else:
            return False
    
    def print_progress(self, val_map):
        #kl, nll = self.get_grads()
        progress = int(float(self._current_step) / self._n_train_batch * 100)
        speed = float(self._current_step * self._batch_size) / (time.time() - self._begin_time) * self._n_devices
        unit = "token" if self._dataset.batch_type() == "token" else "batch"
        #sys.stdout.write("[epoch {}|{}%] elbo={:.2f} | {:.1f} {}/s   \r".format(
        #    self._current_epoch + 1, progress, val_map["elbo"], speed, unit,
        #))
        sys.stdout.write("[epoch {}|{}%] elbo={:.2f} | kl {:.2f} nll {:.2f} {:.1f} {}/s   \r".format(
            self._current_epoch + 1, progress, val_map["elbo"], val_map["kl"], val_map["nll"], speed, unit,
        ))
        sys.stdout.flush()
    
    def log(self, who, msg):
        line = "[{}] {}".format(who, msg)
        self._log_lines.append(line)
        if self._is_root_node():
            print(line)
            if self._dev_writer is not None:
                self._dev_writer.add_text(who, msg)

    def save(self, path=None):
        """Save the trainer to the given file path.
        """
        state_dict = {
            "epoch": self._current_epoch,
            "step": self._current_step,
            "global_step": self._global_step,
            "model_state": self._model.state_dict(),
            "inner_optimizer_state": self.inner_optimizer.state_dict(),
            "outer_optimizer_state": self.outer_optimizer.state_dict(),
            "leanring_rate": self.learning_rate()
        }
        if path is None:
            path = self._save_path
        if path is not None:
            torch.save(state_dict, path)
            open(self._save_path + ".log", "w").writelines([l + "\n" for l in self._log_lines])
    
    def load(self, path=None):
        if path is None:
            path = self._save_path
        first_param = next(self._model.parameters())
        device_str = str(first_param.device)
        state_dict = torch.load(path, map_location=device_str)
        self._model.load_state_dict(state_dict["model_state"])
        self.inner_optimizer.load_state_dict(state_dict["inner_optimizer_state"])
        self.outer_optimizer.load_state_dict(state_dict["outer_optimizer_state"])
        self._current_step = state_dict["step"]
        self._current_epoch = state_dict["epoch"]
        if "global_step" in state_dict:
            self._global_step = state_dict["global_step"]
        # Manually setting learning rate may be redundant?
        if "learning_rate" in state_dict:
            self.set_learning_rate(state_dict["learning_rate"])
    
    def is_finished(self):
        is_finished = self._scheduler.is_finished()
        if is_finished and self._dev_writer is not None:
            self._train_writer.close()
            self._dev_writer.close()
        if self._multigpu and self._horovod:
            import horovod.torch as hvd
            flag_tensor = torch.tensor(1 if is_finished else 0)
            flag_tensor = hvd.broadcast(flag_tensor, ROOT_RANK)
            return flag_tensor > 0
        else:
            return is_finished
    
    def learning_rate(self):
        return self.outer_optimizer.param_groups[0]["lr"]
    
    def synchronize_learning_rate(self):
        """Synchronize learning rate over all devices.
        """
        if self._multigpu and self._horovod:
            import horovod.torch as hvd
            lr = torch.tensor(self.learning_rate())
            lr = hvd.broadcast(lr, ROOT_RANK)
            new_lr = float(lr.numpy())
            if new_lr != self.learning_rate():
                self.set_learning_rate(new_lr, silent=True)
        
    def set_learning_rate(self, lr, silent=False):
        for g in self.outer_optimizer.param_groups:
            g["lr"] = lr
        if self._is_root_node() and not silent:
            self.log("nmtlab", "change learning rate to {:.6f}".format(lr))
    
    def record_train_scores(self, scores):
        for k, val in scores.items():
            self._train_scores[k] += float(val.cpu())
        self._train_count += 1
        
    def begin_epoch(self, epoch):
        """Set current epoch.
        """
        self._current_epoch = epoch
        self._scheduler.before_epoch()
        self._begin_time = time.time()
        self._train_count = 0
        self._train_scores.clear()
    
    def end_epoch(self):
        """End one epoch.
        """
        self._scheduler.after_epoch()
        for k in self._train_scores:
            self._train_scores[k] /= self._train_count
        self.log("train", self._dict_str(self._train_scores))
        self.log("nmtlab", "Ending epoch {}, spent {} minutes  ".format(
            self._current_epoch + 1, int(self.epoch_time() / 60.)
        ))
    
    def begin_step(self, step):
        """Set current step.
        """
        self._current_step = step
        self._scheduler.before_step()
        # if "trains_task" in OPTS and OPTS.trains_task is not None:
        #     OPTS.trains_task.set_last_iteration(step)
    
    def epoch(self):
        """Get current epoch.
        """
        return self._current_epoch
    
    def step(self):
        """Get current step.
        """
        return self._current_step
    
    def global_step(self):
        """Get global step.
        """
        return self._global_step
    
    def model(self):
        """Get model."""
        return self._model
    
    def devices(self):
        """Get the number of devices (GPUS).
        """
        return self._n_devices
    
    def epoch_time(self):
        """Get the seconds consumed in current epoch.
        """
        return time.time() - self._begin_time
    
    def _report_valid_data_hash(self):
        """Report the hash number of the valid data.

        This is to ensure the valid scores are consistent in every runs.
        """
        if not isinstance(self._dataset, MTDataset):
            return
        import hashlib
        valid_list = [
            " ".join(example.tgt)
            for example in self._dataset.raw_valid_data().examples
        ]
        valid_hash = hashlib.sha1("\n".join(valid_list).encode("utf-8", "ignore")).hexdigest()[-8:]
        self.log("nmtlab", "Validation data has {} samples, with hash {}".format(len(valid_list), valid_hash))

    def _clip_grad_norm(self):
        """Clips gradient norm of parameters.
        """
        if self._clip_norm <= 0:
            return
        parameters = filter(lambda p: p.grad is not None, self._model.parameters())
        max_norm = float(self._clip_norm)
        for param in parameters:
            grad_norm = param.grad.data.norm()
            if grad_norm > max_norm:
                param.grad.data.mul_(max_norm / (grad_norm + 1e-6))
            
    @staticmethod
    def _compute_bleu(sampled_tokens, tgt_seq):
        """Compute smoothed BLEU of sampled tokens
        """
        bleus = []
        tgt_seq = tgt_seq.cpu().numpy()
        sampled_tokens = sampled_tokens.cpu().numpy()
        tgt_mask = np.greater(tgt_seq, 0)
        for i in xrange(tgt_seq.shape[0]):
            target_len = int(tgt_mask[i].sum())
            ref_tokens = tgt_seq[i, 1:target_len - 1]
            out_tokens = list(sampled_tokens[i, 1:target_len - 1])
            if not out_tokens:
                bleus.append(0.)
            else:
                bleus.append(smoothed_bleu(out_tokens, ref_tokens))
        return np.mean(bleus)

    def using_horovod(self):
        return self._horovod
    
    @staticmethod
    def _dict_str(rmap):
        return " ".join(
            ["{}={:.2f}".format(n, v) for n, v in rmap.items()]
        )
    
    def _is_root_node(self):
        if self._multigpu and self._horovod:
            import horovod.torch as hvd
            return hvd.rank() == ROOT_RANK
        else:
            return True


