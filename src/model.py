# Requirements
from typing import Dict, List
import torch
from transformers import AutoConfig, AutoModel

# Utils

## Data formatting and projection
class LinearProjFormat(torch.nn.Module):
    def __init__(self,
                 tags:List,
                 hidden_size:int,
                 proj_dim:int=128,
                 ):
        super(LinearProjFormat, self).__init__()
        # Parameters
        self.linear = {'IC':torch.nn.Linear(hidden_size, proj_dim),
                        'H_NER':{column:torch.nn.Linear(hidden_size, proj_dim) for column in tags},
                        }
        self.tags = tags
    
    def forward(self, backbone_tensor):
        formatted_output = {"IC":self.linear['IC'](backbone_tensor),
                            "H_NER":{column:self.linear['H_NER'][column](backbone_tensor) for column in self.tags},
                            }
        return formatted_output


## Information sharing from IC to NER
class IC2NER(torch.nn.Module):
    def __init__(self,
                 tags:List,
                 num_labels:Dict,
                 pos_embeddings:int,
                 proj_dim:int=128,
                 num_heads:int=4,
                 ):
        super(IC2NER, self).__init__()
        # Parameters
        self.tags = tags
        # IC reshape
        self.linear = {column: torch.nn.Linear(pos_embeddings,num_labels['H_NER'][column]) for column in self.tags}
        # H_NER multi-head attn products
        self.attn = {column:torch.nn.MultiheadAttention(proj_dim, num_heads, kdim=proj_dim, vdim=proj_dim, batch_first=True) for column in self.tags[:-1]}
    
    def forward(self, formatted_tensor):
        # IC branch setup
        input = formatted_tensor.copy()
        input['IC'] = {column:self.linear[column](torch.transpose(input['IC'], 1,2)) for column in self.tags}
        # H_NER tensors
        for i in range(len(self.tags)-1):
            input['H_NER'][self.tags[i+1]], _ = self.attn[self.tags[i]](query=input['H_NER'][self.tags[i]],
                                                                        key=input['H_NER'][self.tags[i+1]],
                                                                        value=input['H_NER'][self.tags[i+1]],
                                                                        )
        # Mix
        ner_output = {column: torch.matmul(input['H_NER'][column], input['IC'][column]) for column in self.tags}
        return ner_output, input['H_NER'][self.tags[-1]]


## Information sharing from NER to IC
class NER2IC(torch.nn.Module):
    def __init__(self,
                 num_labels:Dict,
                 proj_dim:int=128,
                 ):
        super(NER2IC, self).__init__()
        # Parameters
        self.linear = torch.nn.Linear(proj_dim, num_labels['IC'])
    
    def forward(self, ic_tensor, last_ner_tensor):
        ic_tensor = torch.mean(ic_tensor, dim=-1, keepdim=True) # Shape (batch_size, pos_embeddings, 1)
        last_ner_tensor = torch.mean(last_ner_tensor, dim=1, keepdim=True) # Shape (batch_size, 1, proj_dim)
        output = torch.matmul(ic_tensor, last_ner_tensor) # Shape (batch_size, pos_embeddings, proj_dim)
        output = torch.mean(output, dim=1, keepdim=False) # Shape (batch_size, proj_dim)
        output = self.linear(output) # Shape (batch_size, num_classes['IC'])
        return output

# Model
class MT_IC_HNER_Model(torch.nn.Module):
    def __init__(self,
                 model_name:str,
                 num_labels:Dict,
                 proj_dim:int=128,
                 num_heads:int=4,
                 hidden_dropout_prob:float=.1,
                 layer_norm_eps:float=1e-7,
                 ):
        super(MT_IC_HNER_Model, self).__init__()
        # Parameters
        self.num_heads = num_heads
        self.num_labels = num_labels
        self.proj_dim = proj_dim
        self.tags = list(self.num_labels["H_NER"].keys())
        # Take pretrained model from custom configuration
        config = AutoConfig.from_pretrained(model_name)
        config.update(
            {
                "output_hidden_states": True,
                "hidden_dropout_prob": hidden_dropout_prob,
                "layer_norm_eps": layer_norm_eps,
                "add_pooling_layer": False,
            }
        )
        self.pos_embeddings = config.max_position_embeddings
        self.hidden_size = config.hidden_size
        self.transformer = AutoModel.from_config(config)
        # Format
        self.format_layer = LinearProjFormat(self.tags, self.hidden_size, self.proj_dim)
        # NER manipulation
        self.ic2ner = IC2NER(self.tags, self.num_labels, self.pos_embeddings, self.proj_dim, self.num_heads)
        # IC ensemble
        self.ner2ic = NER2IC(self.num_labels, self.proj_dim)
    
    def forward(self, input):
        transformer_output = self.transformer(input["tokens"], input["attn_mask"])
        sequence_output = transformer_output.last_hidden_state # Shape (batch, max_position_embeddings, hidden_size)
        formatted_dict = self.format_layer(sequence_output)
        ner_output, last_ner = self.ic2ner(formatted_dict)
        ic_output = self.ner2ic(formatted_dict['IC'], last_ner)
        return {'IC':ic_output,
                'H_NER':ner_output,
                }
