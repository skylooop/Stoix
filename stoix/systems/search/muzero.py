import copy
import functools
import time
from typing import Any, Callable, Dict, Tuple

import chex
import flashbax as fbx
import flax
import hydra
import jax
import jax.numpy as jnp
import mctx
import optax
import rlax
import tensorflow_probability.substrates.jax as tfp
from colorama import Fore, Style
from flashbax.buffers.trajectory_buffer import BufferState
from flax.core.frozen_dict import FrozenDict
from jumanji.env import Environment
from jumanji.types import TimeStep
from omegaconf import DictConfig, OmegaConf
from rich.pretty import pprint

from stoix.networks.base import FeedForwardActor as Actor
from stoix.networks.base import FeedForwardCritic as Critic
from stoix.networks.inputs import EmbeddingInput
from stoix.networks.model_based import Dynamics, Representation
from stoix.systems.ppo.types import ActorCriticParams
from stoix.systems.search.evaluator import search_evaluator_setup
from stoix.systems.search.types import (
    AZTransition,
    DynamicsApply,
    MZLearnerState,
    MZOptStates,
    MZParams,
    RepresentationApply,
    RootFnApply,
    SearchApply,
    WorldModelParams,
)
from stoix.types import (
    ActorApply,
    CriticApply,
    ExperimentOutput,
    LearnerFn,
    LogEnvState,
)
from stoix.utils import make_env as environments
from stoix.utils.checkpointing import Checkpointer
from stoix.utils.jax_utils import unreplicate_batch_dim, unreplicate_n_dims
from stoix.utils.logger import LogEvent, StoixLogger
from stoix.utils.multistep import batch_n_step_bootstrapped_returns
from stoix.utils.total_timestep_checker import check_total_timesteps
from stoix.utils.training import make_learning_rate
from stoix.wrappers.episode_metrics import get_final_step_metrics

tfd = tfp.distributions


def make_root_fn(
    representation_apply_fn: RepresentationApply,
    actor_apply_fn: ActorApply,
    critic_apply_fn: CriticApply,
) -> RootFnApply:
    def root_fn(
        params: MZParams,
        observation: chex.ArrayTree,
        state_embedding: chex.ArrayTree,  # This is the state of the environment
    ) -> mctx.RootFnOutput:
        observation_embedding = representation_apply_fn(
            params.world_model_params.representation_params, observation
        )

        pi = actor_apply_fn(params.prediction_params.actor_params, observation_embedding)
        value = critic_apply_fn(params.prediction_params.critic_params, observation_embedding)
        logits = pi.logits

        root_fn_output = mctx.RootFnOutput(
            prior_logits=logits,
            value=value,
            embedding=observation_embedding,
        )

        return root_fn_output

    return root_fn


def make_recurrent_fn(
    dynamics_apply_fn: DynamicsApply,
    actor_apply_fn: ActorApply,
    critic_apply_fn: CriticApply,
) -> mctx.RecurrentFn:
    def recurrent_fn(
        params: MZParams,
        rng_key: chex.PRNGKey,
        action: chex.Array,
        state_embedding: chex.ArrayTree,
    ) -> Tuple[mctx.RecurrentFnOutput, chex.ArrayTree]:

        next_state_embedding, next_reward = dynamics_apply_fn(
            params.world_model_params.dynamics_params, state_embedding, action
        )

        pi = actor_apply_fn(params.prediction_params.actor_params, next_state_embedding)
        value = critic_apply_fn(params.prediction_params.critic_params, next_state_embedding)
        logits = pi.logits

        recurrent_fn_output = mctx.RecurrentFnOutput(
            reward=next_reward,
            discount=jnp.ones_like(next_reward),
            prior_logits=logits,
            value=value,
        )

        return recurrent_fn_output, next_state_embedding

    return recurrent_fn


def get_warmup_fn(
    env: Environment,
    params: MZParams,
    apply_fns: Tuple[
        RepresentationApply, DynamicsApply, ActorApply, CriticApply, RootFnApply, SearchApply
    ],
    buffer_add_fn: Callable,
    config: DictConfig,
) -> Callable:

    representation_apply_fn, _, _, critic_apply_fn, root_fn, search_apply_fn = apply_fns

    def warmup(
        env_states: LogEnvState, timesteps: TimeStep, buffer_states: BufferState, keys: chex.PRNGKey
    ) -> Tuple[LogEnvState, TimeStep, BufferState, chex.PRNGKey]:
        def _env_step(
            carry: Tuple[LogEnvState, TimeStep, chex.PRNGKey], _: Any
        ) -> Tuple[Tuple[LogEnvState, TimeStep, chex.PRNGKey], AZTransition]:
            """Step the environment."""

            env_state, last_timestep, key = carry
            # SELECT ACTION
            key, policy_key = jax.random.split(key)
            root = root_fn(params, last_timestep.observation, env_state.env_state)
            search_output = search_apply_fn(params, policy_key, root)
            action = search_output.action
            search_policy = search_output.action_weights
            search_value = search_output.search_tree.node_values[:, mctx.Tree.ROOT_INDEX]
            state_embedding = representation_apply_fn(
                params.world_model_params.representation_params, last_timestep.observation
            )
            behaviour_value = critic_apply_fn(
                params.prediction_params.critic_params, state_embedding
            )

            # STEP ENVIRONMENT
            env_state, timestep = jax.vmap(env.step, in_axes=(0, 0))(env_state, action)

            # LOG EPISODE METRICS
            done = timestep.last().reshape(-1)
            info = timestep.extras["episode_metrics"]

            transition = AZTransition(
                done,
                action,
                behaviour_value,
                timestep.reward,
                search_value,
                search_policy,
                last_timestep.observation,
                info,
            )

            return (env_state, timestep, key), transition

        # STEP ENVIRONMENT FOR ROLLOUT LENGTH
        (env_states, timesteps, keys), traj_batch = jax.lax.scan(
            _env_step, (env_states, timesteps, keys), None, config.system.warmup_steps
        )

        # Add the trajectory to the buffer.
        # Swap the batch and time axes.
        traj_batch = jax.tree_map(lambda x: jnp.swapaxes(x, 0, 1), traj_batch)
        buffer_states = buffer_add_fn(buffer_states, traj_batch)

        return env_states, timesteps, keys, buffer_states

    batched_warmup_step: Callable = jax.vmap(
        warmup, in_axes=(0, 0, 0, 0), out_axes=(0, 0, 0, 0), axis_name="batch"
    )

    return batched_warmup_step


def get_learner_fn(
    env: Environment,
    apply_fns: Tuple[
        RepresentationApply, DynamicsApply, ActorApply, CriticApply, RootFnApply, SearchApply
    ],
    update_fns: Tuple[optax.TransformUpdateFn, optax.TransformUpdateFn, optax.TransformUpdateFn],
    buffer_fns: Tuple[Callable, Callable],
    config: DictConfig,
) -> LearnerFn[MZLearnerState]:
    """Get the learner function."""

    # Get apply and update functions for actor and critic networks.
    (
        representation_apply_fn,
        dynamics_apply_fn,
        actor_apply_fn,
        critic_apply_fn,
        root_fn,
        search_apply_fn,
    ) = apply_fns
    world_model_update_fn, actor_update_fn, critic_update_fn = update_fns
    buffer_add_fn, buffer_sample_fn = buffer_fns

    def _update_step(learner_state: MZLearnerState, _: Any) -> Tuple[MZLearnerState, Tuple]:
        """A single update of the network."""

        def _env_step(learner_state: MZLearnerState, _: Any) -> Tuple[MZLearnerState, AZTransition]:
            """Step the environment."""
            params, opt_states, buffer_state, key, env_state, last_timestep = learner_state

            # SELECT ACTION
            key, policy_key = jax.random.split(key)
            root = root_fn(params, last_timestep.observation, env_state.env_state)
            search_output = search_apply_fn(params, policy_key, root)
            action = search_output.action
            search_policy = search_output.action_weights
            search_value = search_output.search_tree.node_values[:, mctx.Tree.ROOT_INDEX]
            state_embedding = representation_apply_fn(
                params.world_model_params.representation_params, last_timestep.observation
            )
            behaviour_value = critic_apply_fn(
                params.prediction_params.critic_params, state_embedding
            )

            # STEP ENVIRONMENT
            env_state, timestep = jax.vmap(env.step, in_axes=(0, 0))(env_state, action)

            # LOG EPISODE METRICS
            done = timestep.last().reshape(-1)
            info = timestep.extras["episode_metrics"]

            transition = AZTransition(
                done,
                action,
                behaviour_value,
                timestep.reward,
                search_value,
                search_policy,
                last_timestep.observation,
                info,
            )
            learner_state = MZLearnerState(
                params, opt_states, buffer_state, key, env_state, timestep
            )
            return learner_state, transition

        # STEP ENVIRONMENT FOR ROLLOUT LENGTH
        learner_state, traj_batch = jax.lax.scan(
            _env_step, learner_state, None, config.system.rollout_length
        )
        params, opt_states, buffer_state, key, env_state, last_timestep = learner_state

        # Add the trajectory to the buffer.
        # Swap the batch and time axes.
        traj_batch = jax.tree_map(lambda x: jnp.swapaxes(x, 0, 1), traj_batch)
        buffer_state = buffer_add_fn(buffer_state, traj_batch)

        def _update_epoch(update_state: Tuple, _: Any) -> Tuple:
            def _actor_loss_fn(
                actor_params: FrozenDict,
                representation_params: FrozenDict,
                sequence: AZTransition,
            ) -> Tuple:
                """Calculate the actor loss."""
                # RERUN NETWORK
                state_embedding = representation_apply_fn(representation_params, sequence.obs)
                actor_policy = actor_apply_fn(actor_params, state_embedding)

                # CALCULATE LOSS
                actor_loss = (
                    tfd.Categorical(probs=sequence.search_policy).kl_divergence(actor_policy).mean()
                )
                entropy = actor_policy.entropy().mean()

                total_loss_actor = actor_loss - config.system.ent_coef * entropy
                return total_loss_actor, (actor_loss, entropy)

            def _critic_loss_fn(
                critic_params: FrozenDict,
                representation_params: FrozenDict,
                sequence: AZTransition,
            ) -> Tuple:
                """Calculate the critic loss."""
                # RERUN NETWORK
                state_embedding = representation_apply_fn(representation_params, sequence.obs)
                pred_value = critic_apply_fn(critic_params, state_embedding)[:, :-1]
                r_t = sequence.reward[:, :-1]
                d_t = 1.0 - sequence.done.astype(jnp.float32)
                d_t = (d_t * config.system.gamma).astype(jnp.float32)
                d_t = d_t[:, :-1]
                search_values = sequence.search_value[:, 1:]

                n_step_returns = batch_n_step_bootstrapped_returns(
                    r_t, d_t, search_values, config.system.n_steps
                )

                value_loss = rlax.l2_loss(pred_value, n_step_returns).mean()

                critic_total_loss = config.system.vf_coef * value_loss
                return critic_total_loss, (value_loss)

            def _world_model_loss_fn(
                world_model_params: WorldModelParams,
                sequence: AZTransition,
            ) -> Tuple:
                """Calculate the world model loss."""

                state_embedding = representation_apply_fn(
                    world_model_params.representation_params, sequence.obs
                )[
                    :, 0
                ]  # B, T=0

                def unroll_fn(
                    state_embedding_and_params: Tuple[chex.Array, FrozenDict], action: chex.Array
                ) -> Tuple[chex.Array, chex.Array]:
                    state_embedding, dynamics_params = state_embedding_and_params
                    next_state_embedding, reward = dynamics_apply_fn(
                        dynamics_params, state_embedding, action
                    )
                    return (next_state_embedding, dynamics_params), reward

                action = jnp.swapaxes(sequence.action, 0, 1)  # T, B
                _, predicted_rewards = jax.lax.scan(
                    unroll_fn, (state_embedding, world_model_params.dynamics_params), action
                )
                predicted_rewards = jnp.swapaxes(predicted_rewards, 0, 1)  # B, T

                reward_loss = rlax.l2_loss(predicted_rewards, sequence.reward)

                # Mask the loss to ensure that auto-reset states are not incuded in the loss
                # discounts = 1.0 - sequence.done.astype(jnp.float32)
                # mask = jnp.cumprod(discounts, axis=-1)
                # reward_loss = jnp.sum(reward_loss * mask) / (jnp.sum(mask) + 1)
                # ELSE
                reward_loss = jnp.mean(reward_loss)

                return reward_loss, (reward_loss)

            params, opt_states, buffer_state, key = update_state

            key, sample_key = jax.random.split(key)

            # SAMPLE SEQUENCES
            sequence_sample = buffer_sample_fn(buffer_state, sample_key)
            sequence: AZTransition = sequence_sample.experience

            # CALCULATE ACTOR LOSS
            actor_grad_fn = jax.value_and_grad(_actor_loss_fn, has_aux=True)
            actor_loss_info, actor_grads = actor_grad_fn(
                params.prediction_params.actor_params,
                params.world_model_params.representation_params,
                sequence,
            )

            # CALCULATE CRITIC LOSS
            critic_grad_fn = jax.value_and_grad(_critic_loss_fn, has_aux=True)
            critic_loss_info, critic_grads = critic_grad_fn(
                params.prediction_params.critic_params,
                params.world_model_params.representation_params,
                sequence,
            )

            # CALCULATE WORLD MODEL LOSS
            world_model_grad_fn = jax.value_and_grad(_world_model_loss_fn, has_aux=True)
            world_model_loss_info, world_model_grads = world_model_grad_fn(
                params.world_model_params, sequence
            )

            # Compute the parallel mean (pmean) over the batch.
            # This calculation is inspired by the Anakin architecture demo notebook.
            # available at https://tinyurl.com/26tdzs5x
            # This pmean could be a regular mean as the batch axis is on the same device.
            actor_grads, actor_loss_info = jax.lax.pmean(
                (actor_grads, actor_loss_info), axis_name="batch"
            )
            # pmean over devices.
            actor_grads, actor_loss_info = jax.lax.pmean(
                (actor_grads, actor_loss_info), axis_name="device"
            )

            critic_grads, critic_loss_info = jax.lax.pmean(
                (critic_grads, critic_loss_info), axis_name="batch"
            )
            # pmean over devices.
            critic_grads, critic_loss_info = jax.lax.pmean(
                (critic_grads, critic_loss_info), axis_name="device"
            )

            world_model_grads, world_model_loss_info = jax.lax.pmean(
                (world_model_grads, world_model_loss_info), axis_name="batch"
            )
            # pmean over devices.
            world_model_grads, world_model_loss_info = jax.lax.pmean(
                (world_model_grads, world_model_loss_info), axis_name="device"
            )

            # UPDATE ACTOR PARAMS AND OPTIMISER STATE
            actor_updates, actor_new_opt_state = actor_update_fn(
                actor_grads, opt_states.actor_opt_state
            )
            actor_new_params = optax.apply_updates(
                params.prediction_params.actor_params, actor_updates
            )

            # UPDATE CRITIC PARAMS AND OPTIMISER STATE
            critic_updates, critic_new_opt_state = critic_update_fn(
                critic_grads, opt_states.critic_opt_state
            )
            critic_new_params = optax.apply_updates(
                params.prediction_params.critic_params, critic_updates
            )

            # UPDATE WORLD MODEL PARAMS AND OPTIMISER STATE
            world_model_updates, world_model_new_opt_state = world_model_update_fn(
                world_model_grads, opt_states.world_model_opt_state
            )
            world_model_new_params = optax.apply_updates(
                params.world_model_params, world_model_updates
            )

            # PACK NEW PARAMS AND OPTIMISER STATE
            new_prediction_params = ActorCriticParams(actor_new_params, critic_new_params)
            new_params = MZParams(new_prediction_params, world_model_new_params)
            new_opt_state = MZOptStates(
                actor_new_opt_state, critic_new_opt_state, world_model_new_opt_state
            )

            # PACK LOSS INFO
            total_loss = actor_loss_info[0] + critic_loss_info[0] + world_model_loss_info[0]
            value_loss = critic_loss_info[1]
            actor_loss = actor_loss_info[1][0]
            entropy = actor_loss_info[1][1]
            reward_loss = world_model_loss_info[1]
            loss_info = {
                "total_loss": total_loss,
                "value_loss": value_loss,
                "actor_loss": actor_loss,
                "entropy": entropy,
                "reward_loss": reward_loss,
            }
            return (new_params, new_opt_state, buffer_state, key), loss_info

        update_state = (params, opt_states, buffer_state, key)

        # UPDATE EPOCHS
        update_state, loss_info = jax.lax.scan(
            _update_epoch, update_state, None, config.system.epochs
        )

        params, opt_states, buffer_state, key = update_state
        learner_state = MZLearnerState(
            params, opt_states, buffer_state, key, env_state, last_timestep
        )
        metric = traj_batch.info
        return learner_state, (metric, loss_info)

    def learner_fn(learner_state: MZLearnerState) -> ExperimentOutput[MZLearnerState]:
        """Learner function."""

        batched_update_step = jax.vmap(_update_step, in_axes=(0, None), axis_name="batch")

        learner_state, (episode_info, loss_info) = jax.lax.scan(
            batched_update_step, learner_state, None, config.arch.num_updates_per_eval
        )
        return ExperimentOutput(
            learner_state=learner_state,
            episode_metrics=episode_info,
            train_metrics=loss_info,
        )

    return learner_fn


def parse_search_method(config: DictConfig) -> Any:
    """Parse search method from config."""
    if config.system.search_method.lower() == "muzero":
        search_method = mctx.muzero_policy
    elif config.system.search_method.lower() == "gumbel":
        search_method = mctx.gumbel_muzero_policy
    else:
        raise ValueError(f"Search method {config.system.search_method} not supported.")

    return search_method


def learner_setup(
    env: Environment,
    keys: chex.Array,
    config: DictConfig,
) -> Tuple[LearnerFn[MZLearnerState], RootFnApply, SearchApply, MZLearnerState]:
    """Initialise learner_fn, network, optimiser, environment and states."""
    # Get available TPU cores.
    n_devices = len(jax.devices())

    # Get number of actions and agents.
    num_actions = int(env.action_spec().num_values)
    config.system.action_dim = num_actions

    # PRNG keys.
    key, representation_net_key, dynamics_net_key, actor_net_key, critic_net_key = keys

    # Define network and optimiser.
    actor_torso = hydra.utils.instantiate(config.network.actor_network.pre_torso)
    actor_action_head = hydra.utils.instantiate(
        config.network.actor_network.action_head, action_dim=num_actions
    )
    critic_torso = hydra.utils.instantiate(config.network.critic_network.pre_torso)
    critic_head = hydra.utils.instantiate(config.network.critic_network.critic_head)

    actor_network = Actor(
        torso=actor_torso, action_head=actor_action_head, input_layer=EmbeddingInput()
    )
    critic_network = Critic(
        torso=critic_torso, critic_head=critic_head, input_layer=EmbeddingInput()
    )

    representation_network_torso = hydra.utils.instantiate(
        config.network.representation_network.torso
    )
    representation_embedding_head = hydra.utils.instantiate(
        config.network.representation_network.embedding_head
    )
    representation_network = Representation(
        torso=representation_network_torso, embedding_head=representation_embedding_head
    )
    dynamics_network_torso = hydra.utils.instantiate(config.network.dynamics_network.torso)
    embedding_head = hydra.utils.instantiate(config.network.dynamics_network.embedding_head)
    reward_head = hydra.utils.instantiate(config.network.dynamics_network.reward_head)
    dynamics_input_processor = hydra.utils.instantiate(
        config.network.dynamics_network.input_processor, action_dim=num_actions
    )
    dynamics_network = Dynamics(
        torso=dynamics_network_torso,
        embedding_head=embedding_head,
        reward_head=reward_head,
        input_processor=dynamics_input_processor,
    )

    world_model_lr = make_learning_rate(
        config.system.world_model_lr,
        config,
        config.system.epochs,
    )

    actor_lr = make_learning_rate(
        config.system.actor_lr,
        config,
        config.system.epochs,
    )
    critic_lr = make_learning_rate(
        config.system.critic_lr,
        config,
        config.system.epochs,
    )

    world_model_optim = optax.chain(
        optax.clip_by_global_norm(config.system.max_grad_norm),
        optax.adam(world_model_lr, eps=1e-5),
    )

    actor_optim = optax.chain(
        optax.clip_by_global_norm(config.system.max_grad_norm),
        optax.adam(actor_lr, eps=1e-5),
    )
    critic_optim = optax.chain(
        optax.clip_by_global_norm(config.system.max_grad_norm),
        optax.adam(critic_lr, eps=1e-5),
    )

    # Initialise observation: Select only obs for a single agent.
    init_x = env.observation_spec().generate_value()
    init_a = env.action_spec().generate_value()
    init_x = jax.tree_util.tree_map(lambda x: x[None, ...], init_x)
    init_a = jax.tree_util.tree_map(lambda x: x[None, ...], init_a)

    # Initialise params params and optimiser state.
    representation_params = representation_network.init(representation_net_key, init_x)
    state_embedding = representation_network.apply(representation_params, init_x)
    dynamics_params = dynamics_network.init(dynamics_net_key, state_embedding, init_a)
    world_model_params = WorldModelParams(representation_params, dynamics_params)
    world_model_opt_state = world_model_optim.init(world_model_params)

    actor_params = actor_network.init(actor_net_key, state_embedding)
    actor_opt_state = actor_optim.init(actor_params)

    critic_params = critic_network.init(critic_net_key, state_embedding)
    critic_opt_state = critic_optim.init(critic_params)

    # Pack params.
    prediction_params = ActorCriticParams(actor_params, critic_params)
    params = MZParams(prediction_params, world_model_params)

    representation_network_apply_fn = representation_network.apply
    dynamics_network_apply_fn = dynamics_network.apply
    actor_network_apply_fn = actor_network.apply
    critic_network_apply_fn = critic_network.apply

    root_fn = make_root_fn(
        representation_network_apply_fn, actor_network_apply_fn, critic_network_apply_fn
    )
    model_recurrent_fn = make_recurrent_fn(
        dynamics_network_apply_fn, actor_network_apply_fn, critic_network_apply_fn
    )
    search_method = parse_search_method(config)
    search_apply_fn = functools.partial(
        search_method,
        recurrent_fn=model_recurrent_fn,
        num_simulations=config.system.num_simulations,
        max_depth=config.system.max_depth,
    )

    # Pack apply and update functions.
    apply_fns = (
        representation_network_apply_fn,
        dynamics_network_apply_fn,
        actor_network_apply_fn,
        critic_network_apply_fn,
        root_fn,
        search_apply_fn,
    )
    update_fns = (world_model_optim.update, actor_optim.update, critic_optim.update)

    # Create replay buffer
    dummy_transition = AZTransition(
        done=jnp.array(False),
        action=jnp.array(0),
        value=jnp.array(0.0),
        reward=jnp.array(0.0),
        search_value=jnp.array(0.0),
        search_policy=jnp.zeros((num_actions,)),
        obs=jax.tree_util.tree_map(lambda x: x.squeeze(0), init_x),
        info={"episode_return": 0.0, "episode_length": 0, "is_terminal_step": False},
    )

    buffer_fn = fbx.make_trajectory_buffer(
        max_size=config.system.buffer_size,
        min_length_time_axis=config.system.sample_sequence_length,
        sample_batch_size=config.system.batch_size,
        sample_sequence_length=config.system.sample_sequence_length,
        period=config.system.period,
        add_batch_size=config.arch.num_envs,
    )
    buffer_fns = (buffer_fn.add, buffer_fn.sample)
    buffer_states = buffer_fn.init(dummy_transition)

    # Get batched iterated update and replicate it to pmap it over cores.
    learn = get_learner_fn(env, apply_fns, update_fns, buffer_fns, config)
    learn = jax.pmap(learn, axis_name="device")

    warmup = get_warmup_fn(env, params, apply_fns, buffer_fn.add, config)
    warmup = jax.pmap(warmup, axis_name="device")

    # Initialise environment states and timesteps: across devices and batches.
    key, *env_keys = jax.random.split(
        key, n_devices * config.system.update_batch_size * config.arch.num_envs + 1
    )
    env_states, timesteps = jax.vmap(env.reset, in_axes=(0))(
        jnp.stack(env_keys),
    )
    reshape_states = lambda x: x.reshape(
        (n_devices, config.system.update_batch_size, config.arch.num_envs) + x.shape[1:]
    )
    # (devices, update batch size, num_envs, ...)
    env_states = jax.tree_map(reshape_states, env_states)
    timesteps = jax.tree_map(reshape_states, timesteps)

    # Load model from checkpoint if specified.
    if config.logger.checkpointing.load_model:
        loaded_checkpoint = Checkpointer(
            model_name=config.system.system_name,
            **config.logger.checkpointing.load_args,  # Other checkpoint args
        )
        # Restore the learner state from the checkpoint
        restored_params, _ = loaded_checkpoint.restore_params()
        # Update the params
        params = restored_params

    # Define params to be replicated across devices and batches.
    key, step_keys, warmup_keys = jax.random.split(key, num=3)
    opt_states = MZOptStates(actor_opt_state, critic_opt_state, world_model_opt_state)
    replicate_learner = (params, opt_states, buffer_states, step_keys, warmup_keys)

    # Duplicate learner for update_batch_size.
    broadcast = lambda x: jnp.broadcast_to(x, (config.system.update_batch_size,) + x.shape)
    replicate_learner = jax.tree_map(broadcast, replicate_learner)

    # Duplicate learner across devices.
    replicate_learner = flax.jax_utils.replicate(replicate_learner, devices=jax.devices())

    # Initialise learner state.
    params, opt_states, buffer_states, step_keys, warmup_keys = replicate_learner
    # Warmup the buffer.
    env_states, timesteps, keys, buffer_states = warmup(
        env_states, timesteps, buffer_states, warmup_keys
    )
    init_learner_state = MZLearnerState(
        params, opt_states, buffer_states, step_keys, env_states, timesteps
    )

    return learn, root_fn, search_apply_fn, init_learner_state


def run_experiment(_config: DictConfig) -> None:
    """Runs experiment."""
    config = copy.deepcopy(_config)

    # Calculate total timesteps.
    n_devices = len(jax.devices())
    config = check_total_timesteps(config)
    assert (
        config.arch.num_updates > config.arch.num_evaluation
    ), "Number of updates per evaluation must be less than total number of updates."

    # Create the enviroments for train and eval.
    env, eval_env = environments.make(config=config)

    # PRNG keys.
    key, key_e, representation_key, dynamics_key, actor_net_key, critic_net_key = jax.random.split(
        jax.random.PRNGKey(config.arch.seed), num=6
    )

    # Setup learner.
    learn, root_fn, search_apply_fn, learner_state = learner_setup(
        env, (key, representation_key, dynamics_key, actor_net_key, critic_net_key), config
    )

    # Setup evaluator.
    evaluator, absolute_metric_evaluator, (trained_params, eval_keys) = search_evaluator_setup(
        eval_env=eval_env,
        key_e=key_e,
        search_apply_fn=search_apply_fn,
        root_fn=root_fn,
        params=learner_state.params,
        config=config,
    )

    # Calculate number of updates per evaluation.
    config.arch.num_updates_per_eval = config.arch.num_updates // config.arch.num_evaluation
    steps_per_rollout = (
        n_devices
        * config.arch.num_updates_per_eval
        * config.system.rollout_length
        * config.system.update_batch_size
        * config.arch.num_envs
    )

    # Logger setup
    logger = StoixLogger(config)
    cfg: Dict = OmegaConf.to_container(config, resolve=True)
    cfg["arch"]["devices"] = jax.devices()
    pprint(cfg)

    # Set up checkpointer
    save_checkpoint = config.logger.checkpointing.save_model
    if save_checkpoint:
        checkpointer = Checkpointer(
            metadata=config,  # Save all config as metadata in the checkpoint
            model_name=config.system.system_name,
            **config.logger.checkpointing.save_args,  # Checkpoint args
        )

    # Run experiment for a total number of evaluations.
    max_episode_return = jnp.float32(-1e7)
    best_params = unreplicate_batch_dim(learner_state.params)
    for eval_step in range(config.arch.num_evaluation):
        # Train.
        start_time = time.time()

        learner_output = learn(learner_state)
        jax.block_until_ready(learner_output)

        # Log the results of the training.
        elapsed_time = time.time() - start_time
        t = int(steps_per_rollout * (eval_step + 1))
        episode_metrics, ep_completed = get_final_step_metrics(learner_output.episode_metrics)
        episode_metrics["steps_per_second"] = steps_per_rollout / elapsed_time

        # Separately log timesteps, actoring metrics and training metrics.
        logger.log({"timestep": t}, t, eval_step, LogEvent.MISC)
        if ep_completed:  # only log episode metrics if an episode was completed in the rollout.
            logger.log(episode_metrics, t, eval_step, LogEvent.ACT)
        logger.log(learner_output.train_metrics, t, eval_step, LogEvent.TRAIN)

        # Prepare for evaluation.
        start_time = time.time()
        trained_params = unreplicate_batch_dim(
            learner_output.learner_state.params
        )  # Select only actor params
        key_e, *eval_keys = jax.random.split(key_e, n_devices + 1)
        eval_keys = jnp.stack(eval_keys)
        eval_keys = eval_keys.reshape(n_devices, -1)

        # Evaluate.
        evaluator_output = evaluator(trained_params, eval_keys)
        jax.block_until_ready(evaluator_output)

        # Log the results of the evaluation.
        elapsed_time = time.time() - start_time
        episode_return = jnp.mean(evaluator_output.episode_metrics["episode_return"])

        steps_per_eval = int(jnp.sum(evaluator_output.episode_metrics["episode_length"]))
        evaluator_output.episode_metrics["steps_per_second"] = steps_per_eval / elapsed_time
        # TODO(edan): check this
        eval_episode_metrics = jax.tree_map(
            lambda x: jnp.array(x).squeeze(), evaluator_output.episode_metrics
        )
        logger.log(eval_episode_metrics, t, eval_step, LogEvent.EVAL)

        if save_checkpoint:
            # Save checkpoint of learner state
            checkpointer.save(
                timestep=int(steps_per_rollout * (eval_step + 1)),
                unreplicated_learner_state=unreplicate_n_dims(learner_output.learner_state),
                episode_return=episode_return,
            )

        if config.arch.absolute_metric and max_episode_return <= episode_return:
            best_params = copy.deepcopy(trained_params)
            max_episode_return = episode_return

        # Update runner state to continue training.
        learner_state = learner_output.learner_state

    # Measure absolute metric.
    if config.arch.absolute_metric:
        start_time = time.time()

        key_e, *eval_keys = jax.random.split(key_e, n_devices + 1)
        eval_keys = jnp.stack(eval_keys)
        eval_keys = eval_keys.reshape(n_devices, -1)

        evaluator_output = absolute_metric_evaluator(best_params, eval_keys)
        jax.block_until_ready(evaluator_output)

        elapsed_time = time.time() - start_time
        t = int(steps_per_rollout * (eval_step + 1))
        steps_per_eval = int(jnp.sum(evaluator_output.episode_metrics["episode_length"]))
        evaluator_output.episode_metrics["steps_per_second"] = steps_per_eval / elapsed_time
        logger.log(evaluator_output.episode_metrics, t, eval_step, LogEvent.ABSOLUTE)

    # Stop the logger.
    logger.stop()


@hydra.main(config_path="../../configs", config_name="default_muzero.yaml", version_base="1.2")
def hydra_entry_point(cfg: DictConfig) -> None:
    """Experiment entry point."""
    # Allow dynamic attributes.
    OmegaConf.set_struct(cfg, False)

    # Run experiment.
    run_experiment(cfg)

    print(f"{Fore.CYAN}{Style.BRIGHT}MuZero experiment completed{Style.RESET_ALL}")


if __name__ == "__main__":
    hydra_entry_point()
