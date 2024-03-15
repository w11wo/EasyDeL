import copy
import dataclasses
import os
import sys
import time
import typing

import IPython.display
import termcolor
from fjformer.func.loss_func import cross_entropy_loss_and_accuracy
import wandb

import jax
import flax
from tqdm import tqdm
from ..smi import initialise_tracking, get_mem, get_capacity_matrix
from jax.experimental.pjit import pjit
from jax.sharding import PartitionSpec
from jax import numpy as jnp
from fjformer import (
    match_partition_rules,
    make_shard_and_gather_fns,
    with_sharding_constraint
)
from ..etils.errors import EasyDelTimerError
import chex
from typing import Any, Optional, Callable, Mapping
from ..etils.easystate import EasyDelState
from .base_trainer import TrainerConfigureFunctionFuncOutput
from .causal_language_model_trainer import CausalLanguageModelTrainer
from flax.struct import dataclass

from ..utils import prefix_print


@dataclass
class VisionCausalLanguageModelStepOutput:
    loss: chex.Array

    text_loss: chex.Array
    text_accuracy: chex.Array

    vision_loss: chex.Array
    vision_accuracy: chex.Array


def calculate_accuracy(predictions: chex.Array, targets: chex.Array):
    """
    The calculate_accuracy function takes in two arrays, predictions and targets.
    The function then calculates the accuracy of the model by comparing the predicted classes to
    the target classes. The predicted class is determined by taking argmax along axis - 1 of predictions.
    The correct_predictions variable is an array containing True or False values depending on whether or not
    the prediction was correct for each example in a batch. The total number of examples that were correctly
    predicted are summed up and divided by the total number of examples to get an accuracy value between 0 and 1.

    :param predictions: chex.Array: Pass in the predictions from the model
    :param targets: chex.Array: Calculate the accuracy of the model
    :return: A single value, the accuracy

    """
    predicted_classes = jnp.argmax(predictions, axis=-1)
    correct_predictions = (predicted_classes == targets).sum()
    total_predictions = targets.shape[0]
    accuracy = correct_predictions / total_predictions
    return accuracy


def create_vision_casual_language_model_train_step(partition_spec=PartitionSpec(("dp", "fsdp"), "sp")):
    """
    The create_vision_casual_language_model_train_step function is a training step function that takes in the current state
    of the model,and a batch of data. It then calculates the loss and accuracy for this batch, and returns
    an updated state with new parameters based on these gradients.

    :param partition_spec: Specify which devices the model will be split across
    :return: A casual_language_model_train_step function that takes in the current state of the model,

    """

    def vision_casual_language_model_train_step(state, batch) -> [
        EasyDelState,
        chex.Array,
        VisionCausalLanguageModelStepOutput
    ]:
        """
        The vision_casual_language_model_train_step function is a training step function that takes in the current state
        of the model and a batch of data. It then calculates the loss and accuracy for this batch,
        and returns an updated state with new parameters based on these gradients.

        :param state: Store the model parameters
        :param batch: Pass the data to the model
        :return: A tuple of (state, loss, VisionCausalLanguageModelStepOutput)

        """
        batch = with_sharding_constraint(batch, partition_spec)

        def calculate_loss(params):
            labels = batch.pop("labels")
            label_vision_mask = batch.pop("label_vision_mask")
            logits = state.apply_fn(
                params=params,
                **batch,
                return_dict=True
            ).logits

            vision_loss, vision_accuracy = cross_entropy_loss_and_accuracy(
                logits[:, :-1, :],
                jnp.where(label_vision_mask, labels, 0),
                batch["attention_mask"].astype(jnp.float32)[:, 1:] * label_vision_mask
            )
            text_loss, text_accuracy = cross_entropy_loss_and_accuracy(
                logits[:, :-1, :],
                jnp.where(label_vision_mask, 0, labels),
                batch["attention_mask"].astype(jnp.float32)[:, 1:] * (1.0 - label_vision_mask)
            )

            loss = 0.5 * (vision_loss + text_loss)

            return loss, VisionCausalLanguageModelStepOutput(
                loss=loss,
                text_accuracy=text_accuracy,
                vision_accuracy=vision_accuracy,
                text_loss=text_loss,
                vision_loss=vision_loss
            )

        grad_fn = jax.value_and_grad(calculate_loss, has_aux=True)
        (loss__, metrics), grad = grad_fn(state.params)
        state = state.apply_gradients(grads=grad)
        return state, loss__, metrics

    return vision_casual_language_model_train_step


def create_vision_casual_language_model_evaluation_step(partition_spec=PartitionSpec(("dp", "fsdp"), "sp")):
    """
    The create_vision_casual_language_model_evaluation_step function is used to create a function that calculates the loss
     and accuracy of a model. It takes in a set of parameters, which are then passed into the state.apply_fn function
    to generate logits for each token in the batch. The cross entropy loss and accuracy are then calculated from these
    logits.

    :param partition_spec: Specify the partitioning of the model parameters
    :return: A function that can be used to calculate the loss and accuracy of a model

    """

    def vision_casual_language_model_evaluation_step(state, batch) -> [
        EasyDelState,
        chex.Array,
        VisionCausalLanguageModelStepOutput
    ]:
        """
        The vision_casual_language_model_train_step function is a training step function that takes in the current state
        of the model and a batch of data. It then calculates the loss and accuracy for this batch,
        and returns an updated state with new parameters based on these gradients.

        :param state: Store the model parameters
        :param batch: Pass the data to the model
        :return: A tuple of (state, loss, VisionCausalLanguageModelStepOutput)

        """
        batch = with_sharding_constraint(batch, partition_spec)

        def calculate_loss(params):
            labels = batch.pop("labels")
            label_vision_mask = batch.pop("label_vision_mask")
            logits = state.apply_fn(
                params=params,
                **batch,
                return_dict=True
            ).logits

            vision_loss, vision_accuracy = cross_entropy_loss_and_accuracy(
                logits[:, :-1, :],
                jnp.where(label_vision_mask, labels, 0),
                batch["attention_mask"].astype(jnp.float32)[:, 1:] * label_vision_mask
            )
            text_loss, text_accuracy = cross_entropy_loss_and_accuracy(
                logits[:, :-1, :],
                jnp.where(label_vision_mask, 0, labels),
                batch["attention_mask"].astype(jnp.float32)[:, 1:] * (1.0 - label_vision_mask)
            )

            loss = 0.5 * (vision_loss + text_loss)

            return loss, VisionCausalLanguageModelStepOutput(
                loss=loss,
                text_accuracy=text_accuracy,
                vision_accuracy=vision_accuracy,
                text_loss=text_loss,
                vision_loss=vision_loss
            )

        loss__, metrics = calculate_loss(state.params)
        return loss__, metrics

    return vision_casual_language_model_evaluation_step


@dataclasses.dataclass
class TrainerOutput:
    state: EasyDelState
    mesh: Optional[jax.sharding.Mesh]
    checkpoint_manager: Any
    gather_fns: Optional[Any | Mapping[str, Callable] | dict[Callable]] = None
    shard_fns: Optional[Any | Mapping[str, Callable] | dict[Callable]] = None
    last_save_file_name: Optional[str] = None
    checkpoint_path: Optional[str] = None


class VisionCausalLanguageModelTrainer(CausalLanguageModelTrainer):

    def create_collate_function(
            self,
            max_sequence_length: int,
            truncation_mode: typing.Literal["keep_end", "keep_start"] = "keep_end",
    ) -> Callable:
        def collate_fn(batch):
            results = {}
            corrected_sequence = None
            for key in batch[0].keys():
                if truncation_mode == "keep_end":
                    corrected_sequence = [
                        jnp.array(f[key])[..., -max_sequence_length:] for f in batch
                    ]
                else:
                    corrected_sequence = [
                        jnp.array(f[key])[..., :max_sequence_length] for f in batch
                    ]
                results[key] = jnp.stack(corrected_sequence).reshape(
                    -1,
                    corrected_sequence[0].shape[-1]
                )
            return results

        return collate_fn

    def configure_functions(self) -> TrainerConfigureFunctionFuncOutput:
        """
        The configure_functions function is responsible for configuring the functions that will be used in training.
        It does this by first defining a function called function_configurations, which initializes the model parameters and returns
        them as a EasyDelState object. The EasyDelState object contains all the information needed to train or evaluate
        on a batch of data, including:
        :param self: Access the class attributes
        :return: A TrainerConfigureFunctionFuncOutput object

        """

        def initialize_state_function():
            initialized_parameters = self.model.init_weights(
                jax.random.PRNGKey(0),
                self.arguments.init_input_shape
            )

            if self.arguments.dtype == jnp.bfloat16:
                initialized_parameters = self.model.to_bf16(initialized_parameters)
            elif self.arguments.dtype == jnp.float16:
                initialized_parameters = self.model.to_fp16(initialized_parameters)

            tx = self.tx
            parameters = flax.core.freeze({"params": initialized_parameters})
            tx_init = copy.deepcopy(self.arguments.optimizer_kwargs)

            if self.rapture is not None:
                lora_parameters = self.lora_parameters
                if self.arguments.dtype == jnp.bfloat16:
                    lora_parameters = self.model.to_bf16(lora_parameters)
                elif self.arguments.dtype == jnp.float16:
                    lora_parameters = self.model.to_fp16(lora_parameters)

                return EasyDelState(
                    step=0,
                    apply_fn=self.lora_apply_fn,
                    params=lora_parameters,
                    tx=self.lora_tx,
                    opt_state=self.lora_opt_state,
                    tx_init=EasyDelState.safe_dict(tx_init),
                    hyperparameters=EasyDelState.create_hyperparameters(self.model.config.model_type),
                    module=self.lora_model,
                    module_config=self.model.config,
                    module_config_args=None,
                )
            else:
                return EasyDelState.create(
                    tx=tx,
                    params=parameters,
                    apply_fn=self.model.__call__,
                    module_config=copy.deepcopy(self.model.config),
                    tx_init=tx_init,
                    hyperparameters=EasyDelState.create_hyperparameters(self.model.config.model_type),
                    module=self.model,
                    module_config_args=None
                )

        def create_state_from_params_function(parameters):
            if self.rapture is None:
                return EasyDelState.create(
                    tx=self.tx,
                    params=parameters,
                    apply_fn=self.model.__call__,
                    module_config=copy.deepcopy(self.model.config),
                    tx_init=copy.deepcopy(self.arguments.optimizer_kwargs),
                    hyperparameters=EasyDelState.create_hyperparameters(self.model.config.model_type),
                    module=self.model,
                    module_config_args=None
                )
            else:
                return EasyDelState(
                    step=0,
                    apply_fn=self.lora_apply_fn,
                    params=parameters,
                    tx=self.lora_tx,
                    opt_state=self.lora_opt_state,
                    tx_init=EasyDelState.safe_dict(copy.deepcopy(self.arguments.optimizer_kwargs)),
                    hyperparameters=EasyDelState.create_hyperparameters(self.model.config.model_type),
                    module=self.lora_model,
                    module_config=self.model.config,
                    module_config_args=None,
                )

        state_shape = jax.eval_shape(initialize_state_function)
        state_partition_spec = match_partition_rules(
            self.config.get_partition_rules(
                fully_sharded_data_parallel=self.arguments.fully_sharded_data_parallel
            ) if self.arguments.custom_rule is None else self.arguments.custom_rule,
            state_shape
        )
        create_sharded_state_from_params_function = pjit(
            create_state_from_params_function,
            in_shardings=(state_partition_spec.params,),
            out_shardings=state_partition_spec,
            donate_argnums=(0,)
        )
        sharded_train_step_function = pjit(
            create_vision_casual_language_model_train_step(self.arguments.step_partition_spec),
            in_shardings=(state_partition_spec, PartitionSpec()),
            out_shardings=(state_partition_spec, PartitionSpec(), PartitionSpec()),
            donate_argnums=(0, 0),
        )

        sharded_eval_step_function = pjit(
            create_vision_casual_language_model_evaluation_step(self.arguments.step_partition_spec),
            in_shardings=(state_partition_spec, PartitionSpec()),
            out_shardings=(PartitionSpec(), PartitionSpec()),
            donate_argnums=(0, 0),
        )

        mesh = self.arguments.get_mesh()
        self.arguments.ckpt_path_exists()
        checkpoint_manager = self.arguments.get_streaming_checkpointer()
        self.state_partition_spec = state_partition_spec
        self.state_shape = state_shape

        return TrainerConfigureFunctionFuncOutput(
            create_sharded_state_from_params_function=create_sharded_state_from_params_function,
            sharded_train_step_function=sharded_train_step_function,
            sharded_eval_step_function=sharded_eval_step_function,
            mesh=mesh,
            checkpoint_manager=checkpoint_manager,
            initialize_state_function=initialize_state_function
        )

    def initialize_state(
            self,
            model_parameters: Optional[flax.core.FrozenDict] = None,
            state: Optional[EasyDelState] = None,
    ) -> typing.Tuple[EasyDelState, Mapping[str, Callable], Mapping[str, Callable]]:
        if model_parameters is None and state is None and self.rapture is None and self.checkpoint_path is None:
            raise RuntimeError(
                "You are passing `model_parameters=None`, `state=None`, and `checkpoint_path=None` and also you are not"
                " using LoRA, if you are "
                "Using LoRA make sure to pass parameters and Rapture Config correctly otherwise pass the "
                "model_parameters or state."
            )
        if model_parameters is None and state is None:
            model_parameters = self.lora_parameters
        with self.mesh:
            shard_fns, gather_fns = make_shard_and_gather_fns(
                self.state_partition_spec,
                dtype_specs=self.dtype
            )
            if state is not None:
                sharded_state = state
                params = sharded_state.params if not self.arguments.do_shard_fns else jax.tree_util.tree_map(
                    lambda f, x: f(x),
                    shard_fns.params,
                    sharded_state.params
                )
                sharded_state.params = params
                if sharded_state.opt_state is None:
                    prefix_print(
                        "Action", "Optimizer State is not Found!, initializing one."
                    )
                    with jax.default_device(self.arguments.offload_device):
                        sharded_state = sharded_state.init_opt_state()
                        opt_state = sharded_state.opt_state if not self.arguments.do_shard_fns else jax.tree_util.tree_map(
                            lambda f, x: f(x),
                            shard_fns.opt_state,
                            sharded_state.opt_state
                        )
                        sharded_state = sharded_state.replace(
                            opt_state=opt_state
                        )
            elif self.finetune:

                if model_parameters is None and self.checkpoint_path is not None:
                    prefix_print(
                        "Action", f"Loading Model From {self.checkpoint_path}"
                    )
                    with jax.default_device(self.arguments.offload_device):
                        sharded_state = EasyDelState.load_state(
                            verbose=self.arguments.verbose,
                            state_shard_fns=shard_fns,
                            init_optimizer_state=True,
                            checkpoint_path=self.checkpoint_path,
                        )
                        # sharded_state = sharded_state.replace(
                        #     tx=self.tx,
                        # )
                        state_shape = jax.eval_shape(lambda: sharded_state)
                        state_partition_spec = match_partition_rules(
                            self.config.get_partition_rules(
                                fully_sharded_data_parallel=self.arguments.fully_sharded_data_parallel
                            ) if self.arguments.custom_rule is None else self.arguments.custom_rule,
                            state_shape
                        )
                        sharded_train_step_function = pjit(
                            create_vision_casual_language_model_train_step(
                                partition_spec=self.arguments.step_partition_spec,
                            ),
                            in_shardings=(state_partition_spec, PartitionSpec()),
                            out_shardings=(state_partition_spec, PartitionSpec(), PartitionSpec()),
                            donate_argnums=(0, 0),
                        )

                        sharded_eval_step_function = pjit(
                            create_vision_casual_language_model_evaluation_step(self.arguments.step_partition_spec),
                            in_shardings=(state_partition_spec, PartitionSpec()),
                            out_shardings=(PartitionSpec(), PartitionSpec()),
                            donate_argnums=(0, 0),
                        )

                        self.state_partition_spec = state_partition_spec
                        self.state_shape = state_shape
                        self.sharded_train_step_function = sharded_train_step_function
                        self.sharded_eval_step_function = sharded_eval_step_function

                    if self.arguments.remove_ckpt_after_load:
                        os.remove(self.checkpoint_path)
                elif model_parameters is not None and self.checkpoint_path is None:
                    prefix_print(
                        "Action", f"Sharding Passed Parameters"
                    )
                    from flax.core import unfreeze
                    if not isinstance(model_parameters, flax.core.FrozenDict):
                        prefix_print(
                            "Warning",
                            "Model Parameters should be like FrozenDict({'params': params}) make sure to "
                            "pass as type FrozenDict in case of not getting UnExcepted Errors "
                        )

                    model_parameters = model_parameters if not self.arguments.do_shard_fns else jax.tree_util.tree_map(
                        lambda f, x: f(x),
                        shard_fns.params,
                        model_parameters,
                    )
                    sharded_state = self.create_sharded_state_from_params_function(model_parameters)
                elif model_parameters is not None and self.checkpoint_path is not None:
                    raise EasyDelTimerError(
                        "You can't pass `model_parameters` and `checkpoint_path` at same time"
                    )
                else:
                    raise EasyDelTimerError(
                        "You should pass `model_parameters` or `checkpoint_path` to trainer in order to load model"
                    )
            else:
                sharded_state = self.initialize_state_function()
                params = sharded_state.params if not self.arguments.do_shard_fns else jax.tree_util.tree_map(
                    lambda f, x: f(x),
                    shard_fns.params,
                    sharded_state.params
                )
                sharded_state.params = params

            self.sharded_state = sharded_state
            return sharded_state, shard_fns, gather_fns

    def train(
            self,
            model_parameters: Optional[flax.core.FrozenDict] = None,
            state: Optional[EasyDelState] = None
    ) -> TrainerOutput:
        """
        The train function is the main function of this module.
        It takes a model_parameters argument which can be used to load a pretrained model and finetune it.
        The train function returns an TrainerOutput object that contains the last saved file name, predict func,
        train state, mesh and checkpoint streamer.


        :param self: Make the class methods aware of other methods and attributes within the class
        :param model_parameters: flax.core.FrozenDict: Load a pre-trained model
        :param state: Optional[EasyDelState]: Ready to Use State
        :return: An object of type "TrainerOutput"

        """

        def count_model_parameters(_p):
            termcolor.cprint(
                f"Model Contain {sum(n.size for n in jax.tree_util.tree_flatten(flax.core.unfreeze(_p))[0]) / 1e9} "
                f"Billion Parameters",
                color="red", force_color=True
            )

        dir_prefix: str = "/dev/shm" if sys.platform != "win32" else "."
        checkpoint_path = "SAVING_SKIPPED"
        if self.arguments.track_memory:
            initialise_tracking(dir_prefix=dir_prefix)
        start_time = time.time()
        sharded_state, shard_fns, gather_fns = self.initialize_state(
            model_parameters=model_parameters,
            state=state
        )

        count_model_parameters(sharded_state.params)
        with self.mesh:
            pbar = tqdm(total=self.max_training_steps)
            current_step = int(jax.device_get(sharded_state.step))

            loss_sum = None
            vision_loss_sum = None
            vision_accuracy_sum = None
            text_loss_sum = None
            text_accuracy_sum = None
            pbar.update(sharded_state.step.tolist())
            learning_rates = []
            if self.wandb_runtime is not None:
                model_parameters_number = sum(
                    n.size for n in
                    jax.tree_util.tree_flatten(flax.core.unfreeze(sharded_state.params))[0]
                ) / 1e9
                self.wandb_runtime.log(
                    {
                        "Number of Model Parameters (Billion)": model_parameters_number
                    }
                )
                wandb.summary["Number of Model Parameters (Billion)"] = model_parameters_number
            try:
                for epoch in range(self.arguments.num_train_epochs):
                    for batch in self.dataloader_train:
                        current_step += 1
                        if (
                                self.arguments.step_start_point is not None
                                and
                                self.arguments.step_start_point > current_step
                        ):
                            pbar.update(1)
                        elif current_step < self.max_training_steps:

                            batch["labels"] = batch["input_ids"][..., 1:]
                            for ssb in self.arguments.ids_to_pop_from_dataset:
                                _ = batch.pop(ssb, None)
                            time_s = time.time()
                            outputs_and_metrics: tuple[
                                EasyDelState, chex.Array, VisionCausalLanguageModelStepOutput
                            ] = self.sharded_train_step_function(
                                sharded_state,
                                batch
                            )
                            sharded_state, loss, information_and_accuracies = outputs_and_metrics
                            ttl_time = time.time() - time_s
                            loss_sum = loss.tolist() if loss_sum is None else loss_sum + loss
                            vision_loss = information_and_accuracies.vision_loss
                            vision_accuracy = information_and_accuracies.vision_accuracy
                            text_loss = information_and_accuracies.text_loss
                            text_accuracy = information_and_accuracies.text_accuracy

                            loss_sum = loss.tolist() if loss_sum is None else loss_sum + loss
                            vision_accuracy_sum = vision_accuracy.tolist() if vision_accuracy_sum is None else (
                                    vision_accuracy_sum + vision_accuracy
                            )
                            vision_loss_sum = vision_loss.tolist() if vision_loss_sum is None else (
                                    vision_loss_sum + vision_loss
                            )
                            text_loss_sum = text_loss.tolist() if text_loss_sum is None else text_loss_sum + text_loss
                            text_accuracy_sum = text_accuracy.tolist() if text_accuracy_sum is None else (
                                    text_accuracy_sum + text_accuracy
                            )
                            learning_rates.append(self.scheduler(current_step).tolist())
                            if self.arguments.track_memory:
                                mem_res = get_mem(dir_prefix=dir_prefix)
                            else:
                                mem_res = "Tracking Option is OFF"
                            pbar.update(1)
                            trained_tokens = (
                                    current_step *
                                    self.arguments.total_batch_size *
                                    self.arguments.gradient_accumulation_steps *
                                    self.arguments.max_sequence_length
                            )

                            total_roved_steps = (current_step - self.arguments.step_start_point)

                            train_metrics = {

                                "loss": loss.tolist(),
                                "mean_loss": loss_sum / total_roved_steps,

                                "vision_accuracy": vision_accuracy,
                                "vision_loss": vision_loss,
                                "text_loss": text_loss,
                                "text_accuracy": text_accuracy,

                                "mean_vision_accuracy": vision_accuracy_sum / total_roved_steps,
                                "mean_vision_loss": vision_loss_sum / total_roved_steps,
                                "mean_text_loss": text_loss_sum / total_roved_steps,
                                "mean_text_accuracy": text_accuracy_sum / total_roved_steps,

                                "learning_rate": self.scheduler(int(jax.device_get(sharded_state.step))).tolist(),
                                "step": int(jax.device_get(sharded_state.step)),
                                "step_time": ttl_time,
                                "perplexity": jnp.exp(loss).tolist(),
                                "trained_tokens": trained_tokens,
                                "accelerators": information_queries,
                                "epoch": epoch
                            }
                            if self.wandb_runtime is not None:

                                information_queries = {}
                                if self.arguments.track_memory:
                                    for key in ["Used", "Usage Percent"]:
                                        for device, info in get_capacity_matrix(dir_prefix=dir_prefix).items():
                                            information_queries[f"{device.replace('_', ' ')} ({key})"] = float(
                                                info[key].replace("%", "").replace("GB", ""))

                                with jax.spmd_mode("allow_all"):
                                    self.wandb_runtime.log(
                                        train_metrics
                                    ),
                                    wandb.summary["captured_memory_log"] = mem_res

                            if self.arguments.track_memory:
                                IPython.display.clear_output(True)
                                pbar.display(mem_res)

                            log_metrics = copy.deepcopy(train_metrics)
                            _ = log_metrics.pop("accelerators")
                            pbar.set_postfix(
                                **log_metrics
                            )
                            if self.arguments.training_time is not None:
                                if time.time() - start_time > self.arguments.training_time:
                                    raise EasyDelTimerError("Time Out")
                        else:
                            break
                        if self.arguments.save_steps is not None and current_step % self.arguments.save_steps == 0:
                            if self.rapture is None:
                                filename = self._save_state(
                                    state=sharded_state,
                                    gather_fns=gather_fns,
                                    milestone=True
                                )
                                checkpoint_path = f"{str(self.arguments.get_path())}/{filename}"
                            else:
                                print(
                                    termcolor.colored(
                                        "Info : ", color="red", force_color=True
                                    ),
                                    termcolor.colored(
                                        "You can not use `save_steps` while using LoRA "
                                        "right now. this action will be skipped", color="white", force_color=True
                                    )
                                )
            except KeyboardInterrupt:
                termcolor.cprint(
                    "KeyboardInterrupt At training model Will return Current State of the Model with Parameters.",
                    color="cyan",
                    force_color=True
                )

            except EasyDelTimerError:
                termcolor.cprint(
                    "Training reached out maximum training Time Killing training Process "
                    "and Will return Current State of the Model with Parameters.",
                    color="cyan",
                    force_color=True
                )
            if self.arguments.merge_lora_rapture_parameters and self.rapture is not None:
                print(
                    termcolor.colored(
                        "Info : ", color="red", force_color=True
                    ),
                    termcolor.colored(
                        "Merging LoRA Parameters.", color="white", force_color=True
                    )
                )
                sharded_state = sharded_state.replace(
                    params=self.rapture.merge_parameters(sharded_state.params)
                )
            output = TrainerOutput(
                state=sharded_state,
                mesh=self.mesh,
                shard_fns=shard_fns,
                gather_fns=gather_fns,
                checkpoint_manager=self.checkpoint_manager,
            )
            if self.arguments.save_steps is None and self.arguments.do_last_save:
                shard_fns, gather_fns = make_shard_and_gather_fns(
                    match_partition_rules(
                        self.config.get_partition_rules(
                            fully_sharded_data_parallel=self.arguments.fully_sharded_data_parallel
                        ) if self.arguments.custom_rule is None else self.arguments.custom_rule,
                        jax.eval_shape(lambda: sharded_state)
                    ),
                    dtype_specs=self.dtype
                )  # You have to re-init the new shard and gather functions in order to be able to skip LoRA weight
                # crashing errors and saving errors
                filename = self._save_state(
                    state=sharded_state,
                    gather_fns=gather_fns
                )
                checkpoint_path = f"{str(self.arguments.get_path())}/{filename}"

            if self.arguments.do_eval:
                for _ in self.eval(
                        sharded_state
                ):
                    ...

            output.checkpoint_path = checkpoint_path
            output.last_save_file_name = filename
            wandb.finish()

            return output

    def eval(self, model_state: EasyDelState) -> typing.Iterator[dict]:
        """Evaluate the Given Model State and yield the eval metrics"""
        assert self.dataloader_eval is not None, "`dataloader_eval` is required by evaluator function."
        with self.mesh:

            dir_prefix: str = "/dev/shm" if sys.platform != "win32" else "."

            if self.arguments.track_memory:
                initialise_tracking(dir_prefix=dir_prefix)

            pbar = tqdm(total=self.max_evaluation_steps)
            pbar.set_description("Evaluating")
            current_step = 0
            loss_sum = None
            vision_loss_sum = None
            vision_accuracy_sum = None
            text_loss_sum = None
            text_accuracy_sum = None
            mem_res = "Tracking Option is OFF"

            try:
                for batch in self.dataloader_eval:
                    current_step += 1
                    time_start = time.time()
                    for key in self.arguments.ids_to_pop_from_dataset:
                        _ = batch.pop(key, None)

                    metrics: tuple[chex.Array, VisionCausalLanguageModelStepOutput] = self.sharded_eval_step_function(
                        model_state,
                        batch
                    )
                    total_time = time.time() - time_start
                    (
                        loss, information_and_accuracies
                    ) = metrics

                    vision_loss = information_and_accuracies.vision_loss
                    vision_accuracy = information_and_accuracies.vision_accuracy
                    text_loss = information_and_accuracies.text_loss
                    text_accuracy = information_and_accuracies.text_accuracy

                    loss_sum = loss.tolist() if loss_sum is None else loss_sum + loss
                    vision_accuracy_sum = vision_accuracy.tolist() if vision_accuracy_sum is None else (
                            vision_accuracy_sum + vision_accuracy
                    )
                    vision_loss_sum = vision_loss.tolist() if vision_loss_sum is None else vision_loss_sum + vision_loss
                    text_loss_sum = text_loss.tolist() if text_loss_sum is None else text_loss_sum + text_loss
                    text_accuracy_sum = text_accuracy.tolist() if text_accuracy_sum is None else (
                            text_accuracy_sum + text_accuracy
                    )

                    if self.arguments.track_memory:
                        mem_res = get_mem(dir_prefix=dir_prefix)

                    information_queries = {}
                    if self.arguments.track_memory:
                        for key in ["Used", "Usage Percent"]:
                            for device, info in get_capacity_matrix(dir_prefix=dir_prefix).items():
                                information_queries[f"{device.replace('_', ' ')} ({key})"] = float(
                                    info[key].replace("%", "").replace("GB", ""))

                    total_roved_steps = (current_step - self.arguments.step_start_point)

                    eval_metrics = {
                        "eval_loss": loss.tolist(),
                        "eval_mean_loss": loss_sum / total_roved_steps,

                        "eval_vision_accuracy": vision_accuracy,
                        "eval_vision_loss": vision_loss,
                        "eval_text_loss": text_loss,
                        "eval_text_accuracy": text_accuracy,

                        "eval_mean_vision_accuracy": vision_accuracy_sum / total_roved_steps,
                        "eval_mean_vision_loss": vision_loss_sum / total_roved_steps,
                        "eval_mean_text_loss": text_loss_sum / total_roved_steps,
                        "eval_mean_text_accuracy": text_accuracy_sum / total_roved_steps,

                        "eval_step": current_step,
                        "eval_step_time": total_time,
                        "eval_perplexity": jnp.exp(loss).tolist(),
                        "eval_accelerators": information_queries,
                    }
                    if self.arguments.use_wandb:
                        with jax.spmd_mode("allow_all"):
                            self.wandb_runtime.log(
                                eval_metrics
                            ),
                            wandb.summary["eval_captured_memory_log"] = mem_res

                    if self.arguments.track_memory:
                        IPython.display.clear_output(True)
                        pbar.display(mem_res)
                    pbar.update(1)
                    log_metrics = copy.deepcopy(eval_metrics)
                    _ = log_metrics.pop("eval_accelerators")
                    pbar.set_postfix(
                        **log_metrics
                    )
                    yield eval_metrics
            except KeyboardInterrupt:
                termcolor.cprint(
                    "KeyboardInterrupt At Evaluation model Will return Nothing and just pass.",
                    color="cyan",
                    force_color=True
                )
