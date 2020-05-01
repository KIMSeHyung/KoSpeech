import random
import torch
import torch.nn as nn
import torch.nn.functional as F
from model.beamsearch import BeamSearch
from model.attention import MultiHeadAttention

supported_rnns = {
    'lstm': nn.LSTM,
    'gru': nn.GRU,
    'rnn': nn.RNN
}


def _inflate(tensor, n_repeat, dim):
    repeat_dims = [1] * len(tensor.size())
    repeat_dims[dim] *= n_repeat

    return tensor.repeat(*repeat_dims)


class Speller(nn.Module):
    r"""
    Converts higher level features (from listener) into output utterances
    by specifying a probability distribution over sequences of characters.

    Args:
        num_class (int): the number of classfication
        max_length (int): a maximum allowed length for the sequence to be processed
        hidden_dim (int): the number of features in the hidden state `h`
        sos_id (int): index of the start of sentence symbol
        eos_id (int): index of the end of sentence symbol
        num_layers (int, optional): number of recurrent layers (default: 1)
        rnn_type (str, optional): type of RNN cell (default: gru)
        dropout_p (float, optional): dropout probability for the output sequence (default: 0)
        k (int) : size of beam

    Inputs: inputs, context, teacher_forcing_ratio
        - **inputs** (batch, seq_len, input_size): list of sequences, whose length is the batch size and within which
          each sequence is a list of token IDs.  It is used for teacher forcing when provided. (default `None`)
        - **context** (batch, seq_len, hidden_dim): tensor with containing the outputs of the listener.
          Used for attention mechanism (default is `None`).
        - **teacher_forcing_ratio** (float): The probability that teacher forcing will be used. A random number is
          drawn uniformly from 0-1 for every decoding token, and if the sample is smaller than the given value,
          teacher forcing would be used (default is 0).

    Returns: y_hats, logits
        - **y_hats** (batch, seq_len): predicted y values (y_hat) by the model
        - **logits** (batch, seq_len, num_class): predicted log probability by the model

    Examples::

        >>> speller = Speller(num_class, max_length, hidden_dim, sos_id, eos_id, num_layers)
        >>> y_hats, logits = speller(inputs, context, teacher_forcing_ratio=0.90)
    """

    def __init__(self, num_class, max_length, hidden_dim, sos_id, eos_id, num_head, attn_dim=64,
                 num_layers=1, rnn_type='gru', dropout_p=0.5, device=None, k=5):

        super(Speller, self).__init__()
        assert rnn_type.lower() in supported_rnns.keys(), 'RNN type not supported.'

        self.num_class = num_class
        self.rnn_cell = supported_rnns[rnn_type]
        self.rnn = self.rnn_cell(hidden_dim, hidden_dim, num_layers, batch_first=True, dropout=dropout_p).to(device)
        self.max_length = max_length
        self.hidden_dim = hidden_dim
        self.embedding = nn.Embedding(num_class, self.hidden_dim)
        self.num_layers = num_layers
        self.input_dropout = nn.Dropout(p=dropout_p)
        self.k = k
        self.fc = nn.Linear(self.hidden_dim, num_class)
        self.attention = MultiHeadAttention(in_features=hidden_dim, dim=attn_dim, num_head=num_head)
        self.eos_id = eos_id
        self.sos_id = sos_id
        self.device = device

    def forward_step(self, input_var, h_state, listener_outputs=None):
        embedded = self.embedding(input_var).to(self.device)
        embedded = self.input_dropout(embedded)

        if len(embedded.size()) == 2:  # if beam search
            embedded = embedded.unsqueeze(1)

        if self.training:
            self.rnn.flatten_parameters()

        output, h_state = self.rnn(embedded, h_state)
        context = self.attention(output, listener_outputs)

        predicted_softmax = F.log_softmax(self.fc(context.contiguous().view(-1, self.hidden_dim)), dim=1)
        return predicted_softmax, h_state

    def forward(self, inputs, listener_outputs, teacher_forcing_ratio=0.90, use_beam_search=False):
        y_hats, logits = None, None

        inputs, batch_size, max_length = self._validate_args(inputs, listener_outputs)
        h_state = self.init_state(batch_size)

        use_teacher_forcing = True if random.random() < teacher_forcing_ratio else False
        decode_outputs = list()

        if use_beam_search:
            search = BeamSearch(self, batch_size)
            y_hats = search(inputs, listener_outputs, k=self.k)

        else:
            if use_teacher_forcing:
                inputs = inputs[inputs != self.eos_id].view(batch_size, -1)

                predicted_softmax, h_state = self.forward_step(inputs, h_state, listener_outputs)
                predicted_softmax = predicted_softmax.view(batch_size, inputs.size(1), -1)

                for di in range(predicted_softmax.size(1)):
                    step_output = predicted_softmax[:, di, :]
                    decode_outputs.append(step_output)

            else:
                input_var = inputs[:, 0].unsqueeze(1)

                for di in range(max_length):
                    predicted_softmax, h_state = self.forward_step(input_var, h_state, listener_outputs)
                    # step_output = predicted_softmax.view(batch_size, input_var.size(1), -1).squeeze(1)
                    step_output = predicted_softmax.view(batch_size, 1, -1).squeeze(1)
                    decode_outputs.append(step_output)
                    input_var = decode_outputs[-1].topk(1)[1]

            logits = torch.stack(decode_outputs, dim=1).to(self.device)
            y_hats = logits.max(-1)[1]

        return y_hats, logits

    def init_state(self, batch_size):
        if isinstance(self.rnn, nn.LSTM):
            h_0 = torch.zeros(self.num_layers, batch_size, self.hidden_dim).to(self.device)
            c_0 = torch.zeros(self.num_layers, batch_size, self.hidden_dim).to(self.device)
            h_state = (h_0, c_0)

        else:
            h_state = torch.zeros(self.num_layers, batch_size, self.hidden_dim).to(self.device)

        return h_state

    def _validate_args(self, inputs, listener_outputs):
        batch_size = listener_outputs.size(0)

        if inputs is None:
            inputs = torch.empty(batch_size, 1).type(torch.long)
            inputs[:, 0] = self.sos_id
            max_length = self.max_length

        else:
            max_length = inputs.size(1) - 1  # minus the start of sequence symbol

        return inputs, batch_size, max_length