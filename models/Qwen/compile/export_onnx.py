#!/usr/bin/env python3
# ==============================================================================
#
# Copyright (C) 2023 Sophgo Technologies Inc.  All rights reserved.
#
# TPU-MLIR is licensed under the 2-Clause BSD License except for the
# third-party components.
#
# ==============================================================================

import os
import torch
import argparse
from tqdm import tqdm
from transformers import AutoModelForCausalLM, AutoTokenizer
torch.set_grad_enabled(False)

parser = argparse.ArgumentParser(description='export onnx.')
parser.add_argument('--model_path', type=str, help='path to the torch model.')
parser.add_argument('--device', required=False, type=str, choices=["cpu", "cuda"], default="cuda")
parser.add_argument('--generation_mode', type=str, choices=["basic", "sample", "all"], default="basic", help='mode to generate token.')
# parser.add_argument('--repeat_last_n', type=int, default=50, help='penalize the recent n tokens.')

args = parser.parse_args()

model_path = args.model_path
folder = f"./tmp/onnx"

device = torch.device(args.device)
generation_mode = args.generation_mode

origin_model = AutoModelForCausalLM.from_pretrained(
    model_path, trust_remote_code=True,
    torch_dtype=torch.bfloat16, device_map="auto").eval()

for param in origin_model.parameters():
    param.requires_grad = False

config = origin_model.config
transformer = origin_model.transformer
layers = transformer.h

# REPEAT_LAST_N = args.repeat_last_n
SEQ_LENGTH = config.seq_length
NUM_LAYERS = config.num_hidden_layers
HIDDEN_SIZE = config.hidden_size
NUM_ATTENTION_HEADS = config.num_attention_heads
HEAD_DIM = HIDDEN_SIZE // NUM_ATTENTION_HEADS

print(f'Layers: {NUM_LAYERS}\nHidden size: {HIDDEN_SIZE}\n')

tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)

class Embedding(torch.nn.Module):

    def __init__(self):
        super().__init__()

    def forward(self, input_ids):
        out = transformer.wte(input_ids)
        return out.float()


class QwenBlock(torch.nn.Module):

    def __init__(self, layer_id):
        super().__init__()
        self.layer_id = layer_id
        self.layer = layers[layer_id]
        self.rotary_emb = transformer.rotary_emb(SEQ_LENGTH)
        self.cos_emb = self.rotary_emb[0].view(SEQ_LENGTH, HEAD_DIM)
        self.sin_emb = self.rotary_emb[1].view(SEQ_LENGTH, HEAD_DIM)

    def forward(self, hidden_states, position_ids, attention_mask):
        cos_pos = self.cos_emb[position_ids].unsqueeze(2)
        sin_pos = self.sin_emb[position_ids].unsqueeze(2)
        hidden_states, past_kv = self.layer(
            hidden_states,
            attention_mask=attention_mask,
            rotary_pos_emb_list=[[cos_pos, sin_pos]],
            use_cache=True)
        present_k, present_v = past_kv
        return hidden_states.float(), present_k.float(), present_v.float()


class QwenBlockCache(torch.nn.Module):

    def __init__(self, layer_id):
        super().__init__()
        self.layer_id = layer_id
        self.layer = layers[layer_id]
        self.rotary_emb = transformer.rotary_emb(SEQ_LENGTH)
        self.cos_emb = self.rotary_emb[0].view(SEQ_LENGTH, HEAD_DIM)
        self.sin_emb = self.rotary_emb[1].view(SEQ_LENGTH, HEAD_DIM)

    def forward(self, hidden_states, position_ids, attention_mask, past_k,
                past_v):
        cos_pos = self.cos_emb[position_ids].unsqueeze(2)
        sin_pos = self.sin_emb[position_ids].unsqueeze(2)
        hidden_states, past_kv = self.layer(
            hidden_states,
            layer_past=(past_k, past_v),
            attention_mask=attention_mask,
            rotary_pos_emb_list=[[cos_pos, sin_pos]],
            use_cache=True)
        present_k, present_v = past_kv
        return hidden_states.float(), present_k.float(), present_v.float()


class LmHead(torch.nn.Module):

    def __init__(self):
        super().__init__()

    def forward(self, hidden_states):
        hidden_states = transformer.ln_f(hidden_states)
        m_logits = origin_model.lm_head(hidden_states)
        _, token = torch.topk(m_logits.float(), 1)
        return token


# refs:https://github.com/huggingface/transformers/blob/main/src/transformers/generation/logits_process.py
class LmHeadTopk(torch.nn.Module):

    def __init__(self, top_k = 50, top_p = 0.8, min_tokens_to_keep = 5):
        super().__init__()
        self.top_k = top_k
        self.top_p = top_p
        self.min_tokens_to_keep = min_tokens_to_keep
        self.keep_matrix = torch.zeros((1, self.top_k), dtype=torch.bool)
        self.keep_matrix[0, :self.min_tokens_to_keep] = True

    def forward(self, hidden_states):
        hidden_states = transformer.ln_f(hidden_states)
        m_logits = origin_model.lm_head(hidden_states)
        logits, token = torch.topk(m_logits.float(), self.top_k)

        # top_p
        cumulative_probs = logits.softmax(dim=1).cumsum(dim=1)
        mask = cumulative_probs < self.top_p
        mask = mask + self.keep_matrix
        filtered_logits = torch.where(mask, logits, torch.FloatTensor([-1000.]))
        probs = filtered_logits.softmax(dim=1)
        return probs, token

    
# refs:https://github.com/huggingface/transformers/blob/main/src/transformers/generation/logits_process.py
class LmHeadAll(torch.nn.Module):

    def __init__(self, top_k = 50, min_tokens_to_keep = 5):
        super().__init__()
        self.top_k = top_k
        self.min_tokens_to_keep = min_tokens_to_keep
        self.keep_matrix = torch.zeros((1, self.top_k), dtype=torch.bool)
        self.keep_matrix[0, :self.min_tokens_to_keep] = True

    def forward(self, input_ids, hidden_states, top_p = 0.8, temperature = 1.0, penalty = 1.1):
        hidden_states = transformer.ln_f(hidden_states)
        m_logits = origin_model.lm_head(hidden_states)

        # repeat penalty
        logits = torch.gather(m_logits, 1, input_ids)
        logits = torch.where(logits < 0, logits * penalty, logits / penalty)
        m_logits.scatter_(1, input_ids, logits)

        # top_k
        logits, token = torch.topk(m_logits.float(), self.top_k)

        # temperature
        logits = logits / temperature

        # top_p
        cumulative_probs = logits.softmax(dim=1).cumsum(dim=1)
        mask = cumulative_probs < top_p
        mask = mask + self.keep_matrix
        filtered_logits = torch.where(mask, logits, torch.FloatTensor([-1000.]))
        probs = filtered_logits.softmax(dim=1)
        return probs, token
    

def convert_block(layer_id):
    model = QwenBlock(layer_id)
    hidden_states = torch.randn(
        (1, SEQ_LENGTH, HIDDEN_SIZE)).bfloat16().to(device)
    position_ids = torch.tensor(
        [range(SEQ_LENGTH)], dtype=torch.long).to(device)
    attention_mask = torch.randn(
        (1, 1, SEQ_LENGTH, SEQ_LENGTH)).bfloat16().to(device)
    
    torch.onnx.export(
        model, (hidden_states, position_ids, attention_mask),
        f'{folder}/block_{layer_id}.onnx',
        verbose=False,
        input_names=['input_states', 'position_ids', 'attention_mask'],
        output_names=['hidden_states', 'past_k', 'past_v'],
        do_constant_folding=True,
        opset_version=15)


def convert_block_cache(layer_id):
    model = QwenBlockCache(layer_id)
    hidden_states = torch.randn((1, 1, HIDDEN_SIZE)).bfloat16().to(device)
    position_ids = torch.tensor([range(1)], dtype=torch.long).to(device)
    attention_mask = torch.ones(
        (1, 1, 1, SEQ_LENGTH + 1)).bfloat16().to(device)
    past_k = torch.randn((1, SEQ_LENGTH, NUM_ATTENTION_HEADS, HEAD_DIM)).bfloat16().to(device)
    past_v = torch.randn((1, SEQ_LENGTH, NUM_ATTENTION_HEADS, HEAD_DIM)).bfloat16().to(device)

    torch.onnx.export(
        model, (hidden_states, position_ids, attention_mask, past_k, past_v),
        f'{folder}/block_cache_{layer_id}.onnx',
        verbose=False,
        input_names=[
            'input_states', 'position_ids', 'attention_mask', 'history_k',
            'history_v'
        ],
        output_names=['hidden_states', 'past_k', 'past_v'],
        do_constant_folding=True,
        opset_version=15)


def convert_embedding():
    model = Embedding()
    input_ids = torch.tensor([range(SEQ_LENGTH)]).to(device)
    module = torch.jit.trace(model.forward, input_ids)
    torch.jit.save(module, f'{folder}/embedding.pt')


def convert_lm_head():   
    if generation_mode == "basic":
        model = LmHead()
    elif generation_mode == "sample":
        model = LmHeadTopk()
    elif generation_mode == "all":
        convert_lm_head_all()
        return
    else:
        raise ValueError("generation_mode should be in {}, but we get {}".format(["basic","sample","all"], generation_mode))
    input = torch.randn(1, HIDDEN_SIZE).bfloat16().to(device)
    module = torch.jit.trace(model.forward, input)
    torch.jit.save(module, f'{folder}/lm_head.pt')


def convert_lm_head_all():   
    model = LmHeadAll()
    input_ids = torch.tensor([range(SEQ_LENGTH)]).to(device)
    hidden_states = torch.randn(1, HIDDEN_SIZE).bfloat16().to(device)
    top_p = torch.tensor([0.8]).bfloat16().to(device)
    temperature = torch.tensor([0.98]).bfloat16().to(device)
    penalty = torch.tensor([0.98]).bfloat16().to(device)
    repeat_last_n = torch.tensor([32]).long().to(device)

    module = torch.jit.trace(model.forward, (input_ids, hidden_states, top_p, temperature, penalty))
    torch.jit.save(module, f'{folder}/lm_head.pt')


# create folder to store onnx
if not os.path.exists(folder):
    os.makedirs(folder)

# export models
print(f'Convert block & block_cache')
for i in tqdm(range(NUM_LAYERS)):
    convert_block(i)
    convert_block_cache(i)

print(f'Convert embedding')
convert_embedding()

print(f'Convert lm_head')
convert_lm_head()

