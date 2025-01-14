# ------------------------------------------------------------------------
# Licensed under the Apache License, Version 2.0 (the "License")
# ------------------------------------------------------------------------
# Modified from HoiTransformer (https://github.com/bbepoch/HoiTransformer)
# Copyright (c) Yang Li and Yucheng Tu. All Rights Reserved
# ------------------------------------------------------------------------

import argparse
import datetime
import getpass
import json
import random
import os
import time
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import DataLoader, DistributedSampler

import util.misc as utils
from datasets import build_dataset
from engine import *
from models import build_model

from magic_numbers import *

from torch.utils.tensorboard import SummaryWriter

from models.hoitr import OptimalTransport

# increase ulimit
import resource
rlimit = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (rlimit[1], rlimit[1]))


def create_log_dir(checkpoint='checkpoint', log_path='~'):
    return
    base_dir = os.path.join(log_path, getpass.getuser())
    exp_name = os.path.basename(os.path.abspath('.'))
    log_dir = os.path.join(base_dir, exp_name)
    print(log_dir)
    if not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)
    if not os.path.exists(checkpoint):
        cmd = "ln -s {} {}".format(log_dir, checkpoint)
        os.system(cmd)


def get_args_parser():
    parser = argparse.ArgumentParser('Set transformer detector', add_help=False)
    parser.add_argument('--lr', default=1e-4, type=float)
    parser.add_argument('--lr_backbone', default=1e-5, type=float)
    parser.add_argument('--batch_size', default=2, type=int)
    parser.add_argument('--weight_decay', default=1e-4, type=float)
    parser.add_argument('--epochs', default=250, type=int)
    parser.add_argument('--lr_drop', default=200, type=int)
    parser.add_argument('--clip_max_norm', default=0.1, type=float,
                        help='gradient clipping max norm')

    # Backbone.
    parser.add_argument('--backbone', choices=['resnet50', 'resnet101', 'swin'],
                        required=True,
                        help="Name of the convolutional backbone to use")
    parser.add_argument('--position_embedding', default='sine', type=str,
                        choices=('sine', 'learned'),
                        help="Type of positional embedding to use on top of the image features")

    # Transformer.
    parser.add_argument('--enc_layers', default=6, type=int,
                        help="Number of encoding layers in the transformer")
    parser.add_argument('--dec_layers', default=6, type=int,
                        help="Number of decoding layers in the transformer")
    parser.add_argument('--dec_layers_distance', default=6, type=int)
    parser.add_argument('--dec_layers_occlusion', default=6, type=int)

    parser.add_argument('--dim_feedforward', default=2048, type=int,
                        help="Intermediate size of the feedforward layers in the transformer blocks")
    parser.add_argument('--hidden_dim', default=256, type=int,
                        help="Size of the embeddings (dimension of the transformer)")
    parser.add_argument('--dropout', default=0.1, type=float,
                        help="Dropout applied in the transformer")
    parser.add_argument('--nheads', default=8, type=int,
                        help="Number of attention heads inside the transformer's attentions")
    parser.add_argument('--num_queries', default=100, type=int,
                        help="Number of query slots")
    parser.add_argument('--pre_norm', action='store_true')

    # Loss.
    parser.add_argument('--no_aux_loss', dest='aux_loss', action='store_false',
                        help="Disables auxiliary decoding losses (loss at each layer)")
    # Matcher.
    parser.add_argument('--set_cost_class', default=1, type=float,
                        help="Class coefficient in the matching cost")
    parser.add_argument('--set_cost_bbox', default=5, type=float,
                        help="L1 box coefficient in the matching cost")
    parser.add_argument('--set_cost_giou', default=2, type=float,
                        help="giou box coefficient in the matching cost")

    # Loss coefficients.
    parser.add_argument('--dice_loss_coef', default=1, type=float)
    parser.add_argument('--relation_loss_coef', default=1, type=float)
    parser.add_argument('--bbox_loss_coef', default=5, type=float)
    parser.add_argument('--giou_loss_coef', default=2, type=float)
    parser.add_argument('--eos_coef', default=0.02, type=float,
                        help="Relative classification weight of the no-object class")

    # Dataset parameters.
    parser.add_argument('--dataset_file',
                        choices=['hico', 'vcoco', 'hoia', 'two_point_five_vrd'],
                        required=True)

    # Modify to your log path ******************************* !!!
    exp_time = datetime.datetime.now().strftime('%Y%m%d%H%M')
    create_log_dir(checkpoint='checkpoint', log_path='~/log_path')
    work_dir = 'checkpoint/p_{}'.format(exp_time)

    parser.add_argument('--output_dir', default=work_dir,
                        help='path where to save, empty for no saving')
    parser.add_argument('--device', default='cuda',
                        help='device to use for training')
    parser.add_argument('--seed', default=42, type=int)
    parser.add_argument('--resume', default='', help='resume from checkpoint')
    parser.add_argument('--start_epoch', default=0, type=int, metavar='N',
                        help='start epoch')
    parser.add_argument('--num_workers', default=0, type=int)

    # Distributed training parameters.
    parser.add_argument('--world_size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--dist_url', default='env://',
                        help='url used to set up distributed training')
    parser.add_argument('--swin_model', default='base_cascade',
                        choices=['base_cascade',
                                 'tiny_cascade',
                                 'tiny_maskrcnn',
                                 'small_cascade',
                                 'small_maskrcnn'])
    parser.add_argument('--manual_lr_change', type=float)
    parser.add_argument('--manual_lr_backbone_change', type=float)

    # Experiment name
    parser.add_argument('--experiment_name', default='')

    return parser


def main(args):
    utils.init_distributed_mode(args)
    print(args)
    device = torch.device(args.device)

    print()
    print("GPU_MEMORY_PRESSURE_TEST     ", GPU_MEMORY_PRESSURE_TEST)
    print("USE_SMALL_ANNOTATION_FILE:   ", USE_SMALL_ANNOTATION_FILE)
    print("USE_OPTIMAL_TRANSPORT:       ", USE_OPTIMAL_TRANSPORT)
    print("USE_DEPTH_DURING_TRAINING:   ", USE_DEPTH_DURING_TRAINING)
    print("PREDICT_INTERSECTION_BOX:    ", PREDICT_INTERSECTION_BOX)
    print("USE_RAW_DISTANCE_LABELS:     ", USE_RAW_DISTANCE_LABELS)
    print("USE_RAW_OCCLUSION_LABELS:    ", USE_RAW_OCCLUSION_LABELS)
    print("IMPROVE_INTERMEDIATE_LAYERS: ", IMPROVE_INTERMEDIATE_LAYERS)
    print("CASCADE:                     ", CASCADE)
    if CASCADE:
        print("dec_layers | dec_layers_distance | dec_layers_occlusion: ")
        print(args.dec_layers, "         |", args.dec_layers_distance, "                  |", args.dec_layers_occlusion)
    print("eos_coef:", args.eos_coef)
    print()

    # Create summary writer for tensorboard
    # It will recorde stats such as losses and lr to tensorboard
    writer = SummaryWriter(args.experiment_name)

    # (For debugging purpose) Reduce batch size to 1
    # when debugging on a single image
    if TRAIN_ON_ONE_IMAGE:
        args.batch_size = 1

    # Fix the seed for reproducibility.
    seed = args.seed + utils.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)

    # Build model, hungarian matcher, and optimal transport
    model, criterion = build_model(args)
    model.to(device)
    optimal_transport = OptimalTransport(args)

    # Distributed set up
    model_without_ddp = model

    find_unused_parameters = args.backbone == 'swin'
    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model,
                                                          device_ids=[args.gpu],
                                                          find_unused_parameters=find_unused_parameters)
        model_without_ddp = model.module
    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print('number of params:', n_parameters)
    param_dicts = [
        {"params": [p for n, p in model_without_ddp.named_parameters()
                    if "backbone" not in n and p.requires_grad]},
        {
            "params": [p for n, p in model_without_ddp.named_parameters()
                       if "backbone" in n and p.requires_grad],
            "lr": args.lr_backbone,
        },
    ]

    # Build optimizer and learning rate scheduler
    optimizer = torch.optim.AdamW(param_dicts, lr=args.lr,
                                  weight_decay=args.weight_decay)


    if CYCLIC_SCHEDULER:
        lr_scheduler = torch.optim.lr_scheduler.CyclicLR(optimizer,
                                                         base_lr=CYCLIC_BASE_LR,
                                                         max_lr=CYCLIC_MAX_LR,
                                                         step_size_up = CYCLIC_STEP_SIZE_UP,
                                                         step_size_down=CYCLIC_STEP_SIZE_DOWN,
                                                         mode='triangular2',
                                                         cycle_momentum=False)
    else:
        lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, args.lr_drop)

    # Build datasets
    dataset_train = build_dataset(image_set='train', args=args)
    dataset_valid = build_dataset(image_set='valid', args=args, test_scale=800)
    #dataset_test = build_dataset(image_set='test', args=args, test_scale=800)

    # Build sampler and batch sampler
    if args.distributed:
        sampler_train = DistributedSampler(dataset_train)
        sampler_valid = DistributedSampler(dataset_valid)
        #sampler_test = DistributedSampler(dataset_test)
    else:
        sampler_train = torch.utils.data.RandomSampler(dataset_train)
        sampler_valid = torch.utils.data.RandomSampler(dataset_valid)
        #sampler_test = torch.utils.data.RandomSampler(dataset_test)
    batch_sampler_train = torch.utils.data.BatchSampler(sampler_train,
                                                        args.batch_size,
                                                        drop_last=True)

    # This partially addresses the EOF Error
    torch.multiprocessing.set_sharing_strategy(sharing_strategy)

    def set_worker_sharing_strategy(worker_id: int) -> None:
        torch.multiprocessing.set_sharing_strategy(sharing_strategy)

    data_loader_train = DataLoader(dataset_train,
                                   batch_sampler=batch_sampler_train,
                                   collate_fn=utils.collate_fn,
                                   num_workers=args.num_workers,
                                   worker_init_fn=set_worker_sharing_strategy,
                                   persistent_workers=(PERSISTENT_WORKERS and (args.num_workers > 0)))

    # (For debugging purpose) create a sequential sampler
    sequential_data_loader_train = DataLoader(dataset_train,
                                              args.batch_size,
                                              collate_fn=utils.collate_fn,
                                              num_workers=args.num_workers)
    # Construct batch samplers and data loaders for validation and test sets
    batch_sampler_valid = torch.utils.data.BatchSampler(sampler_valid,
                                                        batch_size_validation,
                                                        drop_last=False)
    data_loader_valid = DataLoader(dataset_valid,
                                   batch_sampler=batch_sampler_valid,
                                   collate_fn=utils.collate_fn,
                                   num_workers=num_workers_validation,
                                   worker_init_fn=set_worker_sharing_strategy)

    # Load from pretrained DETR model.
    if args.num_queries == 100 and args.enc_layers == 6 and args.dec_layers == 6:
        assert args.backbone in ['resnet50', 'resnet101', 'swin'], args.backbone
        if args.backbone == 'resnet50':
            pretrain_model = './data/detr_coco/detr-r50-e632da11.pth'
        elif args.backbone == 'resnet101':
            pretrain_model = './data/detr_coco/detr-r101-2c7b67e5.pth'
        else:
            pretrain_model = None
    else:
        pretrain_model = None
    if pretrain_model is not None:
        pretrain_dict = torch.load(pretrain_model, map_location='cpu')['model']
        my_model_dict = model_without_ddp.state_dict()
        pretrain_dict = {k: v for k, v in pretrain_dict.items() if k in my_model_dict}
        my_model_dict.update(pretrain_dict)
        model_without_ddp.load_state_dict(my_model_dict)

    output_dir = Path(args.output_dir)

    # Resume from checkpoint
    if args.resume:
        checkpoint = torch.load(args.resume, map_location='cpu')
        model_without_ddp.load_state_dict(checkpoint['model'])
        if 'optimizer' in checkpoint \
                and 'lr_scheduler' in checkpoint \
                and 'epoch' in checkpoint:
            optimizer.load_state_dict(checkpoint['optimizer'])
            # Reset lr_scheduler if lr and backbone lr will be manually changed.
            # For example, if I resume using checkpoint of epoch 29,
            # manually change lr, and set lr_drop to 35,
            # then epoch 30--64 will use the manually changed lr,
            # and epoch 65 will use the lr dropped by lr_scheduler
            # Note: these lines of code might lead to unexpected behavior of cyclic lr scheduler.
            if not args.manual_lr_change and not args.manual_lr_backbone_change:
                lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
            args.start_epoch = checkpoint['epoch'] + 1
        if args.manual_lr_change:
            optimizer.param_groups[0]['lr'] = args.manual_lr_change
            print('Changed lr to', args.manual_lr_change)
        if args.manual_lr_backbone_change:
            optimizer.param_groups[1]['lr'] = args.manual_lr_backbone_change
            print('Changed lr_backbone to', args.manual_lr_backbone_change)


    ############################################################################
    # (For debugging purpose)
    # Set data loader for the training set as the sequential one
    if USE_SEQUENTIAL_LOADER:
        data_loader_train = sequential_data_loader_train
    ############################################################################


    print("Start training")
    start_time = time.time()

    for epoch in range(args.start_epoch, args.epochs):
        if args.distributed:
            sampler_train.set_epoch(epoch)
        if epoch == 0 and not USE_SMALL_VALID_ANNOTATION_FILE and not USE_SMALL_ANNOTATION_FILE and not GPU_MEMORY_PRESSURE_TEST:
            # Validate before training
            print("Validate before training:")
            with torch.no_grad():
                validate(args, writer, 'valid', model, criterion, data_loader_valid, optimizer,
                         device, -1, args.clip_max_norm)

        # Train
        train_stats = train_one_epoch(args, writer, model,
                                      criterion, optimal_transport,
                                      data_loader_train,
                                      optimizer, device, epoch,
                                      args.clip_max_norm,
                                      use_optimal_transport=USE_OPTIMAL_TRANSPORT,
                                      lr_scheduler = lr_scheduler)
        lr_scheduler.step()

        # Validate
        with torch.no_grad():
            validate(args, writer, 'valid', model, criterion, data_loader_valid, optimizer,
                     device, epoch, args.clip_max_norm)

        # Test
        # with torch.no_grad():
        #     validate(args, writer, 'test', model, criterion, data_loader_test, optimizer,
        #              device, epoch, args.clip_max_norm)

        # Save Checkpoint
        if args.output_dir:
            checkpoint_name = 'checkpoint_epoch_' + str(epoch) + '.pth'
            checkpoint_paths = [output_dir / checkpoint_name]
            # extra checkpoint before LR drop and every 10 epochs
            if (epoch + 1) % args.lr_drop == 0 or (epoch + 1) % 100 == 0:
                checkpoint_paths.append(output_dir / f'checkpoint{epoch:04}.pth')
            if (epoch + 1) > args.lr_drop and (epoch + 1) % 10 == 0:
                checkpoint_paths.append(output_dir / f'checkpoint{epoch:04}.pth')
            for checkpoint_path in checkpoint_paths:
                utils.save_on_master({
                    'model': model_without_ddp.state_dict(),
                    'optimizer': optimizer.state_dict(),
                    'lr_scheduler': lr_scheduler.state_dict(),
                    'epoch': epoch,
                    'args': args,
                }, checkpoint_path)

        log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                     'epoch': epoch,
                     'n_parameters': n_parameters}

        if args.output_dir and utils.is_main_process():
            with (output_dir / "log.txt").open("a") as f:
                if BACK_PROP_SINKHORN_COST:
                    log_stats = str(log_stats.items())
                f.write(json.dumps(log_stats) + "\n")

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))


if __name__ == '__main__':
    parser = argparse.ArgumentParser('HOI Transformer training script',
                                     parents=[get_args_parser()])
    args = parser.parse_args()
    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    main(args)
