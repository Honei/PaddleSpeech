#!/usr/bin/python3
#! coding:utf-8

# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Train the speaker identify task
"""

import argparse
import os
import re
import json
from paddle import distributed as dist
from paddlespeech.s2t.io.sampler import SortagradDistributedBatchSampler
from paddlespeech.s2t.utils.log import Log
from paddleaudio.datasets.dataset import SpeechDataset
from paddlespeech.s2t.io.sampler import SortagradBatchSampler
from paddlespeech.s2t.io.collator import SimpleCollator
from paddlespeech.vector.models.model import build_sid_models
from paddlespeech.vector.models.model import build_sid_loss
from paddlespeech.vector.training.optimizer import build_optimizers
from paddlespeech.vector.models.model import build_sid_classifier
# from paddlespeech.vector.training.trainer import valid
from paddle.io import DataLoader
from paddle.io import DistributedBatchSampler
from paddle.io import BatchSampler
from yacs.config import CfgNode
from visualdl import LogWriter
from paddlespeech.vector.training.trainer import Trainer
import paddle
import yaml
import time

logger = Log(__name__).getlog()

def train_sp(args, config):
    logger.info("gpu num: {}".format(args.ngpu))
    if (not paddle.is_compiled_with_cuda()) or args.ngpu == 0:
        logger.info("set device: cpu")
        paddle.device.set_device("cpu")
    else:
        logger.info("set device: gpu")
        paddle.device.set_device("gpu")

    world_size = paddle.distributed.get_world_size()
    
    if world_size > 1:
        logger.info("world size: {}".format(world_size))
        paddle.distributed.init_parallel_env()
    
    logger.info(
        f"rank: {dist.get_rank()}, pid: {os.getpid()}, parent_pid: {os.getppid()}",
    )
    logger.info("train metadata: {}".format(args.train_metadata))
    train_metadata = paddle.load(args.train_metadata)
    dev_metadata = paddle.load(args.dev_metadata)
    # # logger.info(train_metadata)
    dev_dataset = SpeechDataset(data=dev_metadata)
    train_dataset = SpeechDataset(data=train_metadata)
    if args.ngpu > 1:
        batch_sampler = SortagradDistributedBatchSampler(
                    train_dataset,
                    batch_size=config.batch_size,
                    num_replicas=None,
                    rank=None,
                    shuffle=True,
                    drop_last=True,
                    sortagrad=config.sortagrad,
                    shuffle_method=config.shuffle_method)
    else:
        batch_sampler = SortagradBatchSampler(
                            train_dataset,
                            shuffle=False,
                            batch_size=config.batch_size,
                            drop_last=True,
                            sortagrad=config.sortagrad,
                            shuffle_method=config.shuffle_method)
    collate_fn_train = SimpleCollator()
    train_loader = DataLoader(
                        train_dataset,
                        batch_sampler=batch_sampler,
                        collate_fn=collate_fn_train,
                        num_workers=1
                        )
    dev_loader = DataLoader(
                        dev_dataset,
                        batch_size=int(config.batch_size),
                        shuffle=False,
                        collate_fn=collate_fn_train,
                        drop_last=False)
    model = build_sid_models(config)
    if world_size > 1:
        model = paddle.DataParallel(model)

    optimizer, lr_scheduler = build_optimizers(model, config)
    loss_fn = build_sid_loss(config)
    classifier = build_sid_classifier(config)
    epoch = 0
    iteration = 0
    # best_loss = float('inf')
    checkpoint_path = os.path.join(args.output_dir, "model")
    writer = LogWriter(os.path.join(args.output_dir, "visualdl"))

    trainer = Trainer()
    while epoch < config.n_epoch:

        trainer.train_batch(model, optimizer, lr_scheduler, 
                            train_loader, classifier, 
                            loss_fn, epoch, writer)
        
        total_loss = trainer.valid(model, dev_loader, classifier, loss_fn, epoch, writer)

        if dist.get_rank() == 0:
            logger.info("start to save the model")
            # if total_loss < best_loss:
            model_dict = model.state_dict()
            opt_dict = optimizer.state_dict()
            params_path = os.path.join(checkpoint_path,  str(epoch) + ".pdparams")
            opt_path = os.path.join(checkpoint_path, str(epoch) + ".pdopt")
            
            paddle.save(model_dict, params_path)
            paddle.save(opt_dict, opt_path)
            logger.info("save the model to {}".format(params_path))
            logger.info("save the optimizer to {}".format(opt_path))
            infos = dict()
            infos.update({
                "step": trainer.iteration,
                "epoch": epoch,
                "lr": optimizer.get_lr(),
                "val_loss": total_loss
            })
            info_path = re.sub('.pdparams$', '.json', params_path)
            infos = {} if infos is None else infos
            with open(info_path, 'w') as fout:
                data = json.dumps(infos)
                fout.write(data)
            logger.info("epoch: {}, val loss: {}".format(epoch, total_loss))
            
        epoch += 1

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", 
                        type=str,
                        help="train speaker identify task config file")
    parser.add_argument("--ngpu", 
                        type=int, 
                        default=1, 
                        help="if ngpu=0, use cpu.")
    parser.add_argument("--train-metadata", 
                        type=str, 
                        help="training data.")
    parser.add_argument("--dev-metadata", 
                        type=str, 
                        help="dev data.")
    parser.add_argument("--output-dir", 
                        default="./exp/",
                        type=str, 
                        help="output dir.")


    args = parser.parse_args()

    with open(args.config) as f:
        config = CfgNode(yaml.safe_load(f))
    logger.info("===========Args=============")
    # logger.info(yaml.safe_dump(vars(args)))
    logger.info(args)
    logger.info("===========Config=============")
    logger.info(config)

    if args.ngpu > 1:
        dist.spawn(train_sp, (args, config), nprocs=args.ngpu)
    else:
        train_sp(args, config)

if __name__ == "__main__":
    main()