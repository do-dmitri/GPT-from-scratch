"""
This script includes code adapted from the following source:
https://github.com/karpathy/build-nanogpt/blob/master/train_gpt2.py

Original code by Andrej Karpathy, licensed under the MIT License.
"""


import os
import time
import torch
import torch.nn.functional as F
from torch.distributed import init_process_group, destroy_process_group
from torch.nn.parallel import DistributedDataParallel as DDP
import torch.distributed as dist
import math
from typing import Optional, Any, Union
from src.dataset.dataloader import DataLoader
import inspect
from torch.optim import Optimizer


class Trainer:
    """
    Trainer class for managing the training process.

    :param model: The model to be trained.
    :param train_loader: The data loader for the training dataset.
    :param val_loader: The data loader for the validation dataset.
    :param raw_model: The original neural network model, unwrapped from DDP if applicable.
    :param n_epochs: The number of epochs.
    :param warmup_iters: Number of iterations for learning rate warmup.
    :param max_iters: Total number of iterations for training.
    :param grad_accum_iters: The number of iterations for gradient accumulation.
    :param max_lr: Maximum learning rate.
    :param min_lr: Minimum learning rate.
    :param use_ddp: Flag to setup DDP or not.
    :param monitor: Flag to monitor the training process.
    :param torch_matmul_percision: Set the precision of floating-point matrix multiplications.
    :param log_dir: Directory to store logs.
    """

    def __init__(self,
                 model: torch.nn.Module,
                 train_loader: DataLoader,
                 val_loader: Optional[DataLoader] = None,
                 raw_model: Optional[torch.nn.Module] = None,
                 n_epochs: int = 1,
                 warmup_iters: int = 715,
                 max_iters: int = 19073,
                 grad_accum_iters: int = 1,
                 max_lr: float = 6e-4,
                 min_lr: float = 6e-3,
                 use_ddp: bool = False,
                 monitor: bool = True,
                 torch_matmul_percision: str = "high",
                 log_dir: str = "log") -> None:
        
        self.model = model
        self.raw_model = raw_model
        self.n_epochs = n_epochs
        self.train_loader = train_loader
        self.val_loader = val_loader
        self.warmup_iters = warmup_iters
        self.max_iters = max_iters
        self.grad_accum_iters = grad_accum_iters
        self.max_lr = max_lr
        self.min_lr = min_lr
        self.use_ddp = use_ddp
        self.monitor = monitor

        torch.set_float32_matmul_precision(torch_matmul_percision)

        self.log_dir = log_dir

        os.makedirs(log_dir, exist_ok=True)
        self.log_file = os.path.join(log_dir, f"log.txt")
        with open(self.log_file, "w") as _:
            pass

        if self.use_ddp:
            self._setup_ddp()
        else:
            self._setup_device()

        self.device_type = "cuda" if self.device.startswith("cuda") else "cpu"

        self.model.to(self.device) 


    def _setup_device(self) -> None:
        """
        Setup the device for training. Use GPU if available, otherwise use CPU.
        """
        if torch.cuda.is_available():
            self.device = "cuda"
        else:
            self.device = "cpu"
        
        self.ddp_rank = 0
        self.ddp_local_rank = 0
        self.ddp_world_size = 1
        self.main_process = True


    def _setup_ddp(self) -> None:
        """
        Setup Distributed Data Parallel (DDP) for training.
        """
        self.ddp_rank = int(os.environ['RANK'])
        self.ddp_local_rank = int(os.environ['LOCAL_RANK'])
        self.ddp_world_size = int(os.environ['WORLD_SIZE'])

        self.device = f'cuda:{self.ddp_local_rank}'
        torch.cuda.set_device(self.device)

        self.main_process = self.ddp_rank == 0


    def train(self) -> None:
        """
        Train the model.
        """
        
        optimizer = self._get_optimizer(weight_decay=0.1, learning_rate=6e-4)

        for e in self.n_epochs:
            self._on_epoch_begin()

            for iter in range(self.max_iters):
                t0 = time.time()

                self.model.train()
                optimizer.zero_grad()
                accumulated_loss = 0.0

                for micro_iter in range(self.grad_accum_iters):
                    x, y = self.train_loader.next_batch()
                    x, y = x.to(self.device), y.to(self.device)

                    if self.use_ddp:
                        self.model.require_backward_grad_sync = (micro_iter == self.grad_accum_iters - 1)
                    with torch.autocast(device_type=self.device_type, dtype=torch.bfloat16):
                        logits = self.model(x, y)
                        loss = F.cross_entropy(logits.view(-1, logits.size(-1)), y.view(-1))

                    loss = loss / self.grad_accum_iters
                    accumulated_loss += loss.detach()
                    loss.backward()
                
                if self.use_ddp:
                    dist.all_reduce(accumulated_loss, op=dist.ReduceOp.AVG)
                
                norm = torch.nn.utils.clip_grad_norm_(self.model.parameters(), 1.0)
                
                lr = self.adjust_optimizer_lr(optimizer, iter)
                
                optimizer.step()

                is_last_step = (iter == self.max_iters - 1)

                if iter % 500 == 0 or is_last_step:
                    self.validate()
                    self.evaluate()
                

                if self.device_type == "cuda":
                    torch.cuda.synchronize() 
                
                dt = time.time() - t0 

                tokens_processed = self.train_loader.cfg.n_batches * self.train_loader.cfg.n_tokens * self.grad_accum_iters * self.ddp_world_size
                tokens_per_sec = tokens_processed / dt

                if self.main_process:
                    if self.monitor:
                        print(f"epoch {e} | iter {iter:5d} | loss: {accumulated_loss.item():.6f} | lr {lr:.4e} | norm: {norm:.4f} | dt: {dt*1000:.2f}ms | tok/sec: {tokens_per_sec:.2f}")
                    with open(self.log_file, "a") as f:
                        f.write(f"epoch {e} | iter {iter} | train loss: {accumulated_loss.item():.6f}\n")

    
    def adjust_optimizer_lr(self, optimizer, iter):
        lr = self.get_lr(iter)
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr
        
        return lr

    def _on_epoch_begin(self):
        self.train_loader.reset()


    @torch.no_grad()
    def validate(self) -> None:
        """
        Validate the model on the validation dataset.
        """
        pass


    @torch.no_grad()
    def evaluate(self) -> None:
        """
        Evaluate the model. 
        """
        pass


    def _get_lr(self, iter: int) -> float:
        """
        Calculate the learning rate at a given iteration.

        :param iter: The current iteration.
        :return: The calculated learning rate.
        """
        if iter < self.warmup_iters:
            return self.max_lr * (iter + 1) / self.warmup_iters
        
        if iter > self.max_iters:
            return self.min_lr
        
        decay_ratio = (iter - self.warmup_iters) / (self.max_iters - self.warmup_iters)

        assert 0 <= decay_ratio <= 1, "Decay ratio out of range."

        coeff = 0.5 * (1.0 + math.cos(math.pi * decay_ratio))

        return self.min_lr + coeff * (self.max_lr - self.min_lr)


    def _get_optimizer(self, weight_decay: float, learning_rate: float) -> Optimizer:
        """
        Get the optimizer for the training process.

        :param weight_decay: The weight decay to be applied to the optimizer.
        :param learning_rate: The learning rate for the optimizer.
        :return: The created AdamW optimizer.
        """
        # Start with all of the candidate parameters (that require grad)
        param_dict = {pn: p for pn, p in self.raw_model.named_parameters()}
        param_dict = {pn: p for pn, p in param_dict.items() if p.requires_grad}

        # Create optim groups. Any parameters that is 2D will be weight decayed, otherwise no.
        # i.e. all weight tensors in matmuls + embeddings decay, all biases and layernorms don't.
        decay_params = [p for n, p in param_dict.items() if p.dim() >= 2]
        nodecay_params = [p for n, p in param_dict.items() if p.dim() < 2]

        optim_groups = [
            {'params': decay_params, 'weight_decay': weight_decay},
            {'params': nodecay_params, 'weight_decay': 0.0}
        ]

        num_decay_params = sum(p.numel() for p in decay_params)
        num_nodecay_params = sum(p.numel() for p in nodecay_params)

        if self.main_process:
            print(f"num decayed parameter tensors: {len(decay_params)}, with {num_decay_params:,} parameters")
            print(f"num non-decayed parameter tensors: {len(nodecay_params)}, with {num_nodecay_params:,} parameters")

        # Create AdamW optimizer and use the fused version if it is available
        fused_available = 'fused' in inspect.signature(torch.optim.AdamW).parameters
        use_fused = fused_available and self.device_type == "cuda"

        if self.main_process:
            print(f"using fused AdamW: {use_fused}")

        optimizer = torch.optim.AdamW(
            optim_groups, 
            lr=learning_rate, 
            betas=(0.9, 0.95), 
            eps=1e-8, 
            fused=use_fused
        )

        return optimizer