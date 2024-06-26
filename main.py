import os
import gc
import numpy as np
import pandas as pd
from tqdm import tqdm

import torch
from torch import nn
import torch.nn.functional as F
from transformers import DistilBertTokenizer

import config as CFG
from dataset import CLIPDataset, get_transforms
from CLIP import *
from utils import AvgMeter, get_lr
from coformer.contrCoformer import *
from coformer.traj_dataset import TrajDataset

def txt2csv():
    df = pd.read_csv("captions.txt")
    df['id'] = [id_ for id_ in range(df.shape[0] // 5) for _ in range(5)]
    df.to_csv(f"{CFG.captions_path}/captions.csv", index=False)

def make_train_valid_dfs():
    dataframe = pd.read_csv(f"{CFG.captions_path}/captions.csv")
    max_id = dataframe["id"].max() + 1 if not CFG.debug else 100
    image_ids = np.arange(0, max_id)
    np.random.seed(42)
    valid_ids = np.random.choice(
        image_ids, size=int(0.2 * len(image_ids)), replace=False
    )
    train_ids = [id_ for id_ in image_ids if id_ not in valid_ids]
    train_dataframe = dataframe[dataframe["id"].isin(train_ids)].reset_index(drop=True)
    valid_dataframe = dataframe[dataframe["id"].isin(valid_ids)].reset_index(drop=True)
    return train_dataframe, valid_dataframe


def build_loaders(dataframe, tokenizer, mode):
    transforms = get_transforms(mode=mode)
    dataset = CLIPDataset(
        dataframe["image"].values,
        dataframe["caption"].values,
        tokenizer=tokenizer,
        transforms=transforms,
    )
    dataloader = torch.utils.data.DataLoader(
        dataset,
        batch_size=CFG.batch_size,
        num_workers=CFG.num_workers,
        shuffle=True if mode == "train" else False,
    )
    return dataloader


def train_epoch(model, train_loader, optimizer, lr_scheduler, step):
    loss_meter = AvgMeter()
    tqdm_object = tqdm(train_loader, total=len(train_loader))
    for batch in tqdm_object:
        batch = {k: v.to(CFG.device) for k, v in batch.items() if k != "caption"}
        loss = model(batch)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        if step == "batch":
            lr_scheduler.step()

        count = batch["image"].size(0)
        loss_meter.update(loss.item(), count)

        tqdm_object.set_postfix(train_loss=loss_meter.avg, lr=get_lr(optimizer))
    return loss_meter


def valid_epoch(model, valid_loader):
    loss_meter = AvgMeter()

    tqdm_object = tqdm(valid_loader, total=len(valid_loader))
    for batch in tqdm_object:
        batch = {k: v.to(CFG.device) for k, v in batch.items() if k != "caption"}
        loss = model(batch)

        count = batch["image"].size(0)
        loss_meter.update(loss.item(), count)

        tqdm_object.set_postfix(valid_loss=loss_meter.avg)
    return loss_meter


def main():
    txt2csv()
    train_df, valid_df = make_train_valid_dfs()
    tokenizer = DistilBertTokenizer.from_pretrained(CFG.text_tokenizer)
    train_loader = build_loaders(train_df, tokenizer, mode="train")
    valid_loader = build_loaders(valid_df, tokenizer, mode="valid")


    model = CLIPModel().to(CFG.device)
    optimizer = torch.optim.AdamW(
        model.parameters(), lr=CFG.lr, weight_decay=CFG.weight_decay
    )
    lr_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode="min", patience=CFG.patience, factor=CFG.factor
    )
    step = "epoch"

    best_loss = float('inf')
    for epoch in range(CFG.epochs):
        print(f"Epoch: {epoch + 1}")
        model.train()
        train_loss = train_epoch(model, train_loader, optimizer, lr_scheduler, step)
        model.eval()
        with torch.no_grad():
            valid_loss = valid_epoch(model, valid_loader)
        
        if valid_loss.avg < best_loss:
            best_loss = valid_loss.avg
            torch.save(model.state_dict(), "best.pt")
            print("Saved Best Model!")

def coformer_build_loaders(data_config):
    dataset = TrajDataset(**data_config)
    dataloader = torch.utils.data.DataLoader(
        dataset,
        batch_size=CFG.batch_size,
        num_workers=CFG.num_workers,
        shuffle=True if data_config["set_type"] == "train" else False,
    )
    return dataloader

def coformer_train_epoch(model, train_loader, optimizer, lr_scheduler, step):
    loss_meter = AvgMeter()
    tqdm_object = tqdm(train_loader, total=len(train_loader))
    for batch in tqdm_object:
        # breakpoint()
        batch_1 = {k: v.to(CFG.device) for k, v in batch['traj_1'].items()}
        batch_2 = {k: v.to(CFG.device) for k, v in batch['traj_2'].items()}
        batch = {'traj_1': batch_1, 'traj_2': batch_2}
        loss = model(batch)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        if step == "batch":
            lr_scheduler.step()

        count = CFG.batch_size
        loss_meter.update(loss.item(), count)

        tqdm_object.set_postfix(train_loss=loss_meter.avg, lr=get_lr(optimizer))
    return loss_meter

def coformer_valid_epoch(model, valid_loader):
    loss_meter = AvgMeter()

    tqdm_object = tqdm(valid_loader, total=len(valid_loader))
    for batch in tqdm_object:
        # breakpoint()
        batch_1 = {k: v.to(CFG.device) for k, v in batch['traj_1'].items()}
        batch_2 = {k: v.to(CFG.device) for k, v in batch['traj_2'].items()}

        encoder_1 = model.first_encoder
        encoder_2 = model.second_encoder
        first_features = encoder_1.encode(**batch_1).squeeze(1)
        second_features = encoder_2.encode(**batch_2).squeeze(1)
        cosine_sim = F.cosine_similarity(first_features, second_features)
        print(cosine_sim)
        print(batch["label"])

        # count = CFG.batch_size
        # loss_meter.update(loss.item(), count)

        # tqdm_object.set_postfix(valid_loss=loss_meter.avg)
    return loss_meter

def coformer_main():
    train_config = CFG.train_config
    valid_config = CFG.valid_config
    train_loader = coformer_build_loaders(train_config)
    valid_loader = coformer_build_loaders(valid_config)

    model = infoNCETrajs().to(CFG.device)
    optimizer = torch.optim.AdamW(
        model.parameters(), lr=CFG.lr, weight_decay=CFG.weight_decay
    )
    lr_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode="min", patience=CFG.patience, factor=CFG.factor
    )
    step = "epoch"
    best_loss = float('inf')
    for epoch in range(CFG.epochs):
        print(f"Epoch: {epoch + 1}")
        model.train()
        train_loss = coformer_train_epoch(model, train_loader, optimizer, lr_scheduler, step)
        print("Train Loss: ", train_loss.avg)
        model.eval()
        with torch.no_grad():
            valid_loss = coformer_valid_epoch(model, valid_loader)
        
        # if valid_loss.avg < best_loss:
        #     best_loss = valid_loss.avg
        #     torch.save(model.state_dict(), "coformer_best.pt")
        #     print("Saved Best Model!")


if __name__ == "__main__":
    # main()
    coformer_main()
