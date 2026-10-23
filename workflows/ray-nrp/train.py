"""Ray Train on NRP: DDP over Parquet shards in NRP S3, checkpoints to the same
bucket, resumed from the latest one after a failure or a resubmission."""

import os
import tempfile

import ray
import ray.train
import ray.train.torch
import torch
from prepare import BUCKET, FEATURES, INPUTS, s3

RUNS = f"{BUCKET}/ray-nrp/runs"


def train_loop(config: dict) -> None:
    model = torch.nn.Sequential(
        torch.nn.Linear(FEATURES, 64), torch.nn.ReLU(), torch.nn.Linear(64, 1)
    )
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
    start = 0
    if checkpoint := ray.train.get_checkpoint():
        with checkpoint.as_directory() as directory:
            state = torch.load(os.path.join(directory, "state.pt"))
        model.load_state_dict(state["model"])
        optimizer.load_state_dict(state["optimizer"])
        start = state["epoch"] + 1
    model = ray.train.torch.prepare_model(model)
    device = ray.train.torch.get_device()
    rank = ray.train.get_context().get_world_rank()
    if rank == 0:
        print(f"starting at epoch {start}", flush=True)

    shard = ray.train.get_dataset_shard("train")
    for epoch in range(start, config["epochs"]):
        for batch in shard.iter_torch_batches(batch_size=4096, device=device):
            x = torch.stack([batch[f"x{i}"] for i in range(FEATURES)], dim=1)
            loss = torch.nn.functional.mse_loss(model(x).squeeze(1), batch["y"])
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = None
            if rank == 0:
                state = {
                    "model": getattr(model, "module", model).state_dict(),
                    "optimizer": optimizer.state_dict(),
                    "epoch": epoch,
                }
                torch.save(state, os.path.join(directory, "state.pt"))
                checkpoint = ray.train.Checkpoint.from_directory(directory)
                print(f"epoch {epoch} loss {loss.item():.4f}", flush=True)
            ray.train.report(
                {"epoch": epoch, "loss": loss.item()}, checkpoint=checkpoint
            )


if __name__ == "__main__":
    trainer = ray.train.torch.TorchTrainer(
        train_loop,
        train_loop_config={"epochs": int(os.environ.get("EPOCHS", "20"))},
        scaling_config=ray.train.ScalingConfig(
            num_workers=int(os.environ["NUM_WORKERS"]), use_gpu=True
        ),
        run_config=ray.train.RunConfig(
            name="ray-nrp-test",
            storage_path=RUNS,
            storage_filesystem=s3(),
            failure_config=ray.train.FailureConfig(max_failures=2),
            checkpoint_config=ray.train.CheckpointConfig(num_to_keep=2),
        ),
        datasets={"train": ray.data.read_parquet(INPUTS, filesystem=s3())},
    )
    print(trainer.fit(), flush=True)
