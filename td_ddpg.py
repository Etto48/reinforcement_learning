import copy
import gymnasium as gym
import torch
import torch.nn as nn
import torch.nn.functional as F
from tqdm import tqdm
import numpy as np
import matplotlib.pyplot as plt
from torch.distributions.multivariate_normal import MultivariateNormal
from get_env_args import get_env_args
from monitor_server import MonitorServer
from models import PolicyModel, CriticModel
import argparse as ap

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
torch.set_default_device(device)

server = MonitorServer()

class ReplayBuffer(torch.utils.data.Dataset):
    def __init__(self, max_len=100000):
        self.buffer = []
        self.max_len = max_len

    def add(self, state, action, reward, next_state, done):
        if len(self.buffer) >= self.max_len:
            random_index = np.random.randint(0, len(self.buffer))
            self.buffer.pop(random_index)
        state = torch.tensor(state, dtype=torch.float32)
        action = torch.tensor(action, dtype=torch.float32)
        reward = torch.tensor(reward, dtype=torch.float32)
        next_state = torch.tensor(next_state, dtype=torch.float32)
        done = torch.tensor(done, dtype=torch.bool)
        self.buffer.append((state, action, reward, next_state, done))

    def clear(self):
        self.buffer.clear()

    def __len__(self):
        return len(self.buffer)
    
    def __getitem__(self, index):
        return self.buffer[index]

class ACNeuralAgent:
    def __init__(self, actor: nn.Module, critic: nn.Module):
        self.actor = actor
        self.target_actor = copy.deepcopy(actor)
        self.actor.eval()
        self.target_actor.eval()
        self.critic = critic
        self.target_critic = copy.deepcopy(critic)
        self.critic.eval()
        self.target_critic.eval()
        self.gamma = 0.99
        self.std = 0.01
        self.replay_buffer = ReplayBuffer()
        self.critic_criterion = nn.MSELoss()
        self.actor_optimizer = torch.optim.AdamW(self.actor.parameters(), lr=1e-4)
        self.critic_optimizer = torch.optim.AdamW(self.critic.parameters(), lr=1e-3)

    def select_action(self, state):
        state = torch.tensor(state, dtype=torch.float32)
        self.actor.eval()
        with torch.no_grad():
            action = self.actor(state)
            action = action.view(-1)
            if self.std > 0:
                action += torch.randn_like(action) * self.std
            action = action.cpu().numpy()
        return action

    def update_target_model(self, tau=0.001):
        for target_param, param in zip(self.target_actor.parameters(), self.actor.parameters()):
            target_param.data.copy_(tau * param.data + (1 - tau) * target_param.data)
        for target_param, param in zip(self.target_critic.parameters(), self.critic.parameters()):
            target_param.data.copy_(tau * param.data + (1 - tau) * target_param.data)

    def experience_replay(self): 
        batch_size = 256
        num_batches = 1
        self.actor.train()
        self.critic.train()
        if len(self.replay_buffer) < num_batches * batch_size:
            return 0, 0
        dl = torch.utils.data.DataLoader(
            self.replay_buffer, 
            batch_size=batch_size, 
            drop_last=True,
            sampler=torch.utils.data.RandomSampler(
                self.replay_buffer, 
                generator=torch.Generator(device=device),
                replacement=True, 
                num_samples=num_batches * batch_size))
        batches = dl
        critic_avg_loss = 0
        actor_avg_loss = 0
        for batch in batches:
            states, actions, rewards, next_states, dones = batch
            
            with torch.no_grad():
                next_actions = self.target_actor(next_states)
                target_q_values = self.target_critic(next_states, next_actions)
                y= rewards + self.gamma * target_q_values.view(-1) * (1 - dones.float())
            
            predicted_q_values = self.critic(states, actions).view(-1)
            critic_loss: torch.Tensor = self.critic_criterion(predicted_q_values, y)

            predicted_actions: torch.Tensor = self.actor(states)
            q_values: torch.Tensor = self.critic(states, predicted_actions)
            actor_loss = -q_values.mean()

            self.actor_optimizer.zero_grad()
            actor_loss.backward()
            torch.nn.utils.clip_grad_norm_(self.actor.parameters(), 0.1)
            self.actor_optimizer.step()
            actor_avg_loss += actor_loss.item()

            self.critic_optimizer.zero_grad()
            critic_loss.backward()
            torch.nn.utils.clip_grad_norm_(self.critic.parameters(), 0.1)
            self.critic_optimizer.step()
            critic_avg_loss += critic_loss.item()

        self.update_target_model()    
        
        critic_avg_loss /= len(batches)
        actor_avg_loss /= len(batches)
        self.critic.eval()
        self.actor.eval()
        return actor_avg_loss, critic_avg_loss

    def iter_to_infinity(self):
        i = 0
        while True:
            yield i
            i += 1

    def fit(self, env: gym.Env, num_episodes: int = 10):
        loading_bar = tqdm(self.iter_to_infinity(), desc="Training")
        best_reward = -np.inf
        for episode in loading_bar:
            state, _ = env.reset()
            episode_reward = 0
            done = False
            steps = 0
            while not done:
                action = self.select_action(state)
                next_state, reward, terminated, truncated, _ = env.step(action)
                if episode % num_episodes == 0 and server.is_connected():
                    screenshot = env.render()
                    server.send(screenshot)
                done = terminated or truncated
                self.replay_buffer.add(state, action, reward, next_state, done)
                policy_loss, critic_loss = self.experience_replay()
                episode_reward += reward
                steps += 1
                state = next_state
            if episode % num_episodes == 0 and server.is_connected():
                server.send_paused()
            best_reward = max(best_reward, episode_reward)
            info = {"Reward": episode_reward, "Best Reward": best_reward, "Actor Loss": policy_loss, "Critic Loss": critic_loss}
            loading_bar.set_postfix(info)
            info["Episode"] = episode
            server.send_info(info)

def main(env_name: str): 
    args = get_env_args(env_name)
    env = gym.make(env_name, **args)
    state_space = env.observation_space
    action_space = env.action_space
    model = PolicyModel(state_space=state_space, action_space=action_space, deterministic=True)
    critic = CriticModel(state_space=state_space, action_space=action_space)
    agent = ACNeuralAgent(actor=model, critic=critic)
    
    agent.fit(env)
    env.close()

if __name__ == "__main__":
    parser = ap.ArgumentParser(description="Deep Deterministic Policy Gradient")
    parser.add_argument("--env", type=str, default="BipedalWalker-v3", required=False, help="Environment to train on")
    args = parser.parse_args()
    main(env_name=args.env)