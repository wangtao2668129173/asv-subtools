# -*- coding:utf-8 -*-

# Copyright xmuspeech (Author: Snowdar 2019-05-29)

import numpy as np

import torch
import torch.nn.functional as F

from .activation import Nonlinearity

from libs.support.utils import to_device
import libs.support.utils as utils


### There are some custom components/layers. ###

## Base ✿
class TdnnAffine(torch.nn.Module):
    """ An implemented tdnn affine component by conv1d
        y = splice(w * x, context) + b

    @input_dim: number of dims of frame <=> inputs channels of conv
    @output_dim: number of layer nodes <=> outputs channels of conv
    @context: a list of context
        e.g.  [-2,0,2]
    If context is [0], then the TdnnAffine is equal to linear layer.
    """
    def __init__(self, input_dim, output_dim, context=[0], bias=True, pad=True, norm_w=False, norm_f=False, subsampling_factor=1):
        super(TdnnAffine, self).__init__()
        
        # Check to make sure the context sorted and has no duplicated values
        for index in range(0, len(context) - 1):
            if(context[index] >= context[index + 1]):
                print("Context tuple is invalid")
                exit()

        self.input_dim = input_dim
        self.output_dim = output_dim
        self.context = context
        self.bool_bias = bias
        self.pad = pad

        self.norm_w = norm_w
        self.norm_f = norm_f

        # It is used to subsample frames with this factor
        self.stride = subsampling_factor

        self.left_context = context[0] if context[0] < 0 else 0 
        self.right_context = context[-1] if context[-1] > 0 else 0 

        self.tot_context = self.right_context - self.left_context + 1

        # Do not support sphereConv now.
        if self.tot_context > 1 and self.norm_f:
            self.norm_f = False
            print("Warning: do not support sphereConv now and set norm_f=False.")

        kernel_size = (self.tot_context,)

        self.weight = torch.nn.Parameter(torch.randn(output_dim, input_dim, *kernel_size))

        if self.bool_bias:
            self.bias = torch.nn.Parameter(torch.randn(output_dim))
        else:
            self.register_parameter('bias', None)

        
        # init weight and bias. It is important
        self.init_weight()

        # Save GPU memory for no skiping case
        if len(context) != self.tot_context:
            # Used to skip some frames index according to context
            self.mask = torch.tensor([[[ 1 if index in context else 0 \
                                        for index in range(self.left_context, self.right_context + 1) ]]])
        else:
            self.mask = None

        ## Deprecated: the broadcast method could be used to save GPU memory, 
        # self.mask = torch.randn(output_dim, input_dim, 0)
        # for index in range(self.left_context, self.right_context + 1):
        #     if index in context:
        #         fixed_value = torch.ones(output_dim, input_dim, 1)
        #     else:
        #         fixed_value = torch.zeros(output_dim, input_dim, 1)

        #     self.mask=torch.cat((self.mask, fixed_value), dim = 2)

        # Save GPU memory of thi case.

        self.selected_device = False

    def init_weight(self):
        # Note, var should be small to avoid slow-shrinking
        torch.nn.init.normal_(self.weight, 0., 0.01)

        if self.bias is not None:
            torch.nn.init.constant_(self.bias, 0.)


    def forward(self, inputs):
        """
        @inputs: a 3-dimensional tensor (a batch), including [samples-index, frames-dim-index, frames-index]
        """
        assert len(inputs.shape) == 3
        assert inputs.shape[1] == self.input_dim

        # Do not use conv1d.padding for self.left_context + self.right_context != 0 case.
        if self.pad:
            inputs = F.pad(inputs, (-self.left_context, self.right_context), mode="constant", value=0)

        assert inputs.shape[2] >=  self.tot_context

        if not self.selected_device and self.mask is not None:
            # To save the CPU -> GPU moving time
            # Another simple case, for a temporary tensor, jus specify the device when creating it.
            # such as, this_tensor = torch.tensor([1.0], device=inputs.device)
            self.mask = to_device(self, self.mask)
            self.selected_device = True

        filters = self.weight  * self.mask if self.mask is not None else self.weight

        if self.norm_w:
            filters = F.normalize(filters, dim=1)

        if self.norm_f:
            inputs = F.normalize(inputs, dim=1)

        outputs = F.conv1d(inputs, filters, self.bias, self.stride, padding=0, dilation=1, groups=1)

        return outputs

    def extra_repr(self):
        return '{input_dim}, {output_dim}, context={context}, bias={bool_bias}, stride={stride}, ' \
               'pad={pad}, norm_w={norm_w}, norm_f={norm_f}'.format(**self.__dict__)


class TdnnfBlock(torch.nn.Module):
    """ Factorized TDNN block w.r.t http://danielpovey.com/files/2018_interspeech_tdnnf.pdf.
    Reference: Povey, D., Cheng, G., Wang, Y., Li, K., Xu, H., Yarmohammadi, M., & Khudanpur, S. (2018). 
               Semi-Orthogonal Low-Rank Matrix Factorization for Deep Neural Networks. Paper presented at the Interspeech.
    Githup Reference: https://github.com/cvqluu/Factorized-TDNN. Note, it maybe have misunderstanding to F-TDNN and 
               I have corrected it w.r.t steps/libs/nnet3/xconfig/composite_layers.py of Kaldi.

    """
    def __init__(self, input_dim, output_dim, inner_size, context_size=0, pad=True):
        super(TdnnfBlock, self).__init__()

        if context_size > 0:
            context_factor1 = [-context_size, 0]
            context_factor2 = [0, context_size]
        else:
            context_factor1 = [0]
            context_factor2 = [0]

        self.factor1 = TdnnAffine(input_dim, inner_size, context_factor1, pad=pad, bias=False)
        self.factor2 = TdnnAffine(inner_size, output_dim, context_factor2, pad=pad, bias=True)

    def forward(self, inputs):
        """
        @inputs: a 3-dimensional tensor (a batch), including [samples-index, frames-dim-index, frames-index]
        """
        assert len(inputs.shape) == 3
        assert inputs.shape[1] == self.input_dim

        return self.factor2(self.factor1(inputs))

    def step(self, epoch, iter):
        pass
        # Updating weight with semi-orthogonal constraint. Note, updating based on backward has no constraint,
        # so we should add the semi-orthogonal constraint here and extrally updating it in training by ourself.

        #self.factor1.step_semi_orth()
        #self.factor2.step_semi_orth()


class GruAffine(torch.nn.Module):
    """xmuspeech (Author: LZ) 2020-02-05
    A GRU affine component.
    """
    def __init__(self, input_dim, output_dim):
        super(GruAffine, self).__init__()

        self.input_dim = input_dim
        self.output_dim = output_dim

        hidden_size = output_dim
        num_directions = 1

        self.hidden_size = hidden_size
        self.num_directions = num_directions

        self.gru = torch.nn.GRU(input_dim, hidden_size)


    def forward(self, inputs):
        """
        @inputs: a 3-dimensional tensor (a batch), including [samples-index, frames-dim-index, frames-index]
        The tensor of inputs in the GRU module is [seq_len, batch, input_size]
        The tensor of outputs in the GRU module is [seq_len, batch, num_directions * hidden_size]
        If the bidirectional is True, num_directions should be 2, else it should be 1.
        """
        assert len(inputs.shape) == 3
        assert inputs.shape[1] == self.input_dim

        inputs = inputs.permute(2,0,1)

        outputs, hn = self.gru(inputs)

        outputs = outputs.permute((1,2,0))

        return outputs


class SoftmaxAffineLayer(torch.nn.Module):
    """ An usual 2-fold softmax layer with an affine transform.
    @dim: which dim to apply softmax on
    """
    def __init__(self, input_dim, output_dim, dim=1, log=True, bias=True, special_init=False):
        super(SoftmaxAffineLayer, self).__init__()

        self.affine = TdnnAffine(input_dim, output_dim, bias=bias)

        if log:
            self.softmax = torch.nn.LogSoftmax(dim=dim)
        else:
            self.softmax = torch.nn.Softmax(dim=dim)

        if special_init :
            torch.nn.init.xavier_uniform_(self.affine.weight, gain=torch.nn.init.calculate_gain('sigmoid'))


    def forward(self, inputs):
        """
        @inputs: any, such as a 3-dimensional tensor (a batch), including [samples-index, frames-dim-index, frames-index]
        """
        return self.softmax(self.affine(inputs))


## ReluBatchNormLayer
class _BaseActivationBatchNorm(torch.nn.Module):
    """[Affine +] Relu + BatchNorm1d.
    Affine could be inserted by a child class.
    """
    def __init__(self):
        super(_BaseActivationBatchNorm, self).__init__()
        self.layers = []
        self.affine = None

    def add_relu_bn(self, output_dim=None, options:dict={}):

        default_params = {
            "bn-relu":False,
            "nonlinearity":'relu',
            "nonlinearity_params":{"inplace":True, "negative_slope":0.01},
            "bn":True,
            "momentum":0.5,
            "affine":False, 
            "special_init":True,
            "mode":'fan_out'
        }

        default_params = utils.assign_params_dict(default_params, options)

        if not default_params["bn-relu"]:
            # relu-bn
            self.activation = Nonlinearity(default_params["nonlinearity"], **default_params["nonlinearity_params"])
            if self.activation is not None:
                self.layers.append(self.activation)

            if default_params["bn"]:
                self.batchnorm = torch.nn.BatchNorm1d(output_dim, momentum = default_params["momentum"], affine = default_params["affine"])
                self.layers.append(self.batchnorm) 
        else:
            # bn-relu
            if default_params["bn"]:
                self.batchnorm = torch.nn.BatchNorm1d(output_dim, momentum = default_params["momentum"], affine = default_params["affine"])
                self.layers.append(self.batchnorm)

            self.activation = Nonlinearity(default_params["nonlinearity"], **default_params["nonlinearity_params"])
            if self.activation is not None:
                self.layers.append(self.activation) 

        if default_params["special_init"] and self.affine is not None:
            if default_params["nonlinearity"] in ["relu", "leaky_relu", "tanh", "sigmoid"]:
                # Before special_init, there is another initial way been done in TdnnAffine and it 
                # is just equal to use torch.nn.init.normal_(self.affine.weight, 0., 0.01) here. 
                torch.nn.init.kaiming_uniform_(self.affine.weight, a=0, mode=default_params["mode"], 
                                               nonlinearity=default_params["nonlinearity"])
            else:
                torch.nn.init.xavier_normal_(self.affine.weight, gain=1.0)

    def forward(self, inputs):
        """
        @inputs: a 3-dimensional tensor (a batch), including [samples-index, frames-dim-index, frames-index]
        """
        return torch.nn.Sequential(*self.layers)(inputs)


class ReluBatchNormTdnnLayer(_BaseActivationBatchNorm):
    """ TDNN-ReLU-BN.
    An usual 3-fold layer with TdnnAffine affine.
    """
    def __init__(self, input_dim, output_dim, context=[0], **options):
        super(ReluBatchNormTdnnLayer, self).__init__()

        affine_options = {
            "bias":True, 
            "norm_w":False,
            "norm_f":False
        }

        affine_options = utils.assign_params_dict(affine_options, options)

        # Only keep the order: affine -> layers.insert -> add_relu_bn,
        # the structure order will be right when print(model), such as follows:
        # (tdnn1): ReluBatchNormTdnnLayer(
        #          (affine): TdnnAffine()
        #          (activation): ReLU()
        #          (batchnorm): BatchNorm1d(512, eps=1e-05, momentum=0.5, affine=False, track_running_stats=True)
        self.affine = TdnnAffine(input_dim, output_dim, context, **affine_options)
        self.layers.insert(0, self.affine)
        self.add_relu_bn(output_dim, options=options)

        # Implement forword function extrally if needed when forword-graph is changed.


class ReluBatchNormTdnnfLayer(_BaseActivationBatchNorm):
    """ F-TDNN-ReLU-BN.
    An usual 3-fold layer with TdnnfBlock affine.
    """
    def __init__(self, input_dim, output_dim, inner_size, context_size = 0, **options):
        super(ReluBatchNormTdnnfLayer, self).__init__()

        self.affine = TdnnfBlock(input_dim, output_dim, inner_size, context_size)
        self.layers.insert(0, self.affine)
        self.add_relu_bn(output_dim, options=options)


## Pooling ✿
class StatisticsPooling(torch.nn.Module):
    """ An usual mean [+ stddev] poolling layer"""
    def __init__(self, input_dim, stddev=True, unbiased=False, eps=1.0e-10):
        super(StatisticsPooling, self).__init__()

        self.stddev = stddev
        self.input_dim = input_dim

        if self.stddev :
            self.output_dim = 2 * input_dim
        else :
            self.output_dim = input_dim

        self.eps = eps
        # Used for unbiased estimate of stddev
        self.unbiased = unbiased

    def forward(self, inputs):
        """
        @inputs: a 3-dimensional tensor (a batch), including [samples-index, frames-dim-index, frames-index]
        """
        assert len(inputs.shape) == 3
        assert inputs.shape[1] == self.input_dim

        # Get the num of frames
        counts = inputs.shape[2]

        mean = torch.unsqueeze(inputs.sum(dim=2) / counts, dim=2)

        if self.stddev :
            if self.unbiased and counts > 1:
                counts = counts - 1

            # The sqrt (as follows) is deprecated because it results in Nan problem.
            # std = torch.unsqueeze(torch.sqrt(torch.sum((inputs - mean)**2, dim=2) / counts), dim=2)
            # There is a eps to solve this problem.
            # Another method: Var is equal to std in "cat" way, actually. So, just use Var directly.

            var = torch.sum((inputs - mean)**2, dim=2) / counts
            std = torch.unsqueeze(torch.sqrt(var.clamp(min=self.eps)), dim=2)
            return torch.cat((mean, std), dim=1)
        else:
            return mean

    def get_output_dim(self):
        return self.output_dim
    
    def extra_repr(self):
        return '{input_dim}, {output_dim}, stddev={stddev}, unbiased={unbiased}, eps={eps}'.format(**self.__dict__)


class AttentionAlphaComponent(torch.nn.Module):
    """ Return alpha for self attention
            alpha = softmax(v'·f(w·x + b) + k)
        where f is relu followed by batchnorm here
    """
    def __init__(self, input_dim, hidden_size=64, context=[0]):
        super(AttentionAlphaComponent, self).__init__()

        self.input_dim = input_dim
        self.hidden_size = hidden_size
        self.relu_affine = ReluBatchNormTdnnLayer(input_dim, hidden_size, context=context)
        # Dim=2 means to apply softmax in different frames-index (batch is a 3-dim tensor in this case)
        self.softmax_affine = SoftmaxAffineLayer(hidden_size, 1, dim=2, log=False, bias=True)
    
    def forward(self, inputs):
        """
        @inputs: a 3-dimensional tensor (a batch), including [samples-index, frames-dim-index, frames-index]
        """
        return self.softmax_affine(self.relu_affine(inputs))

    def extra_repr(self):
        return '{input_dim}, hidden_size={hidden_size}'.format(**self.__dict__)

class AttentiveStatisticsPooling(torch.nn.Module):
    """ An attentive statistics pooling layer according to []"""
    def __init__(self, input_dim, hidden_size=64, context=[0], stddev=True, eps=1.0e-10):
        super(AttentiveStatisticsPooling, self).__init__()

        self.stddev = stddev
        self.input_dim = input_dim

        if self.stddev :
            self.output_dim = 2 * input_dim
        else :
            self.output_dim = input_dim

        self.eps = eps

        self.attention = AttentionAlphaComponent(input_dim, hidden_size, context)

    def forward(self, inputs):
        """
        @inputs: a 3-dimensional tensor (a batch), including [samples-index, frames-dim-index, frames-index]
        """
        assert len(inputs.shape) == 3
        assert inputs.shape[1] == self.input_dim

        alpha = self.attention(inputs)
 
        # Weight avarage
        mean = torch.sum(alpha * inputs, dim=2, keepdim=True)

        if self.stddev :
            var = torch.sum(alpha * inputs**2, dim=2, keepdim=True) - mean**2
            std = torch.sqrt(var.clamp(min=self.eps))
            return torch.cat((mean, std), dim=1)
        else :
            return mean

    def get_output_dim(self):
        return self.output_dim



class LDEPooling(torch.nn.Module):
    """A novel learnable dictionary encoding layer according to [Weicheng Cai, etc., "A NOVEL LEARNABLE 
    DICTIONARY ENCODING LAYER FOR END-TO-END LANGUAGE IDENTIFICATION", icassp, 2018]"""
    def __init__(self, input_dim, c_num=64):
        super(LDEPooling, self).__init__()

        self.input_dim = input_dim
        self.output_dim = input_dim * c_num

        self.mu = torch.nn.Parameter(torch.randn(input_dim, c_num))
        self.s = torch.nn.Parameter(torch.ones(c_num))

        self.softmax_for_w = torch.nn.Softmax(dim=3)

    def forward(self, inputs):
        """
        @inputs: a 3-dimensional tensor (a batch), including [samples-index, frames-dim-index, frames-index]
        """
        assert len(inputs.shape) == 3
        assert inputs.shape[1] == self.input_dim

        r = inputs.transpose(1,2).unsqueeze(3) - self.mu
        w = self.softmax_for_w(self.s * torch.sum(r**2, dim=2, keepdim=True))
        e = torch.mean(w * r, dim=1)

        return e.reshape(-1, self.output_dim, 1)

    def get_output_dim(self):
        return self.output_dim


## Others ✿
class ImportantScale(torch.nn.Module):
    """A based idea to show importantance of every dim of inputs acoustic features.
    """
    def __init__(self, input_dim):
        super(ImportantScale, self).__init__()

        self.input_dim = input_dim
        self.groups = input_dim
        output_dim = input_dim

        kernel_size = (1,)

        self.weight = torch.nn.Parameter(torch.ones(output_dim, input_dim//self.groups, *kernel_size))

    def forward(self, inputs):
        assert len(inputs.shape) == 3
        assert inputs.shape[1] == self.input_dim

        outputs = F.conv1d(inputs, self.weight, bias=None, groups=self.groups)
        return outputs


class ContextDropout(torch.nn.Module):
    """It dropouts in the context dimensionality to achieve two purposes:
           1.make training with random chunk-length
           2.decrease the context dependence to augment the training data
    """
    def __init__(self, p=0.):
        super(ContextDropout, self).__init__()

        self.dropout2d = torch.nn.Dropout2d(p=p)

    def forward(self, intputs):
        """
        @inputs: a 3-dimensional tensor (a batch), including [samples-index, frames-dim-index, frames-index]
        """
        outputs = self.dropout2d(inputs.transpose(1,2)).transpose(1,2)
        return outputs


class AdaptivePCMN(torch.nn.Module):
    """ Using adaptive parametric Cepstral Mean Normalization to replace traditional CMN.
        It is implemented according to [Ozlem Kalinli, etc. "Parametric Cepstral Mean Normalization 
        for Robust Automatic Speech Recognition", icassp, 2019.]
    """
    def __init__(self, input_dim, left_context=-10, right_context=10, pad=True):
        super(AdaptivePCMN, self).__init__()

        assert left_context < 0 and right_context > 0

        self.left_context = left_context
        self.right_context = right_context
        self.tot_context = self.right_context - self.left_context + 1

        kernel_size = (self.tot_context,)

        self.input_dim = input_dim
        # Just pad head and end rather than zeros using replicate pad mode 
        # or set pad false with enough context egs. 
        self.pad = pad
        self.pad_mode = "replicate"

        self.groups = input_dim
        output_dim = input_dim

        # The output_dim is equal to input_dim and keep every dims independent by using groups conv.
        self.beta_w = torch.nn.Parameter(torch.randn(output_dim, input_dim//self.groups, *kernel_size))
        self.alpha_w = torch.nn.Parameter(torch.randn(output_dim, input_dim//self.groups, *kernel_size))
        self.mu_n_0_w = torch.nn.Parameter(torch.randn(output_dim, input_dim//self.groups, *kernel_size))
        self.bias = torch.nn.Parameter(torch.randn(output_dim))

        # init weight and bias. It is important
        self.init_weight()

    def init_weight(self):
        torch.nn.init.normal_(self.beta_w, 0., 0.01)
        torch.nn.init.normal_(self.alpha_w, 0., 0.01)
        torch.nn.init.normal_(self.mu_n_0_w, 0., 0.01)
        torch.nn.init.constant_(self.bias, 0.)

    def forward(self, inputs):
        """
        @inputs: a 3-dimensional tensor (a batch), including [samples-index, frames-dim-index, frames-index]
        """
        assert len(inputs.shape) == 3
        assert inputs.shape[1] == self.input_dim
        assert inputs.shape[2] >= self.tot_context

        if self.pad:
            pad_input = F.pad(inputs, (-self.left_context, self.right_context), mode=self.pad_mode)
        else:
            pad_input = inputs
            inputs = inputs[:,:,-self.left_context:-self.right_context]

        # outputs beta + 1 instead of beta to avoid potentially zeroing out the inputs cepstral features.
        self.beta = F.conv1d(pad_input, self.beta_w, bias=self.bias, groups=self.groups) + 1
        self.alpha = F.conv1d(pad_input, self.alpha_w, bias=self.bias, groups=self.groups)
        self.mu_n_0 = F.conv1d(pad_input, self.mu_n_0_w, bias=self.bias, groups=self.groups)

        outputs = self.beta * inputs - self.alpha * self.mu_n_0

        return outputs


class SEBlock(torch.nn.Module):
    """ A SE Block layer layer which can learn to use global information to selectively emphasise informative 
    features and suppress less useful ones.
    This is a pytorch implementation of SE Block based on the paper:
    Squeeze-and-Excitation Networks
    by JFChou 2019-07-13
    """
    def __init__(self, input_dim, ratio=4):
        '''
        @ratio: a reduction ratio which allows us to vary the capacity and computational cost of the SE blocks 
        in the network.
        '''
        super(SEBlock, self).__init__()

        self.input_dim = input_dim

        self.stats = StatisticsPooling(input_dim, stddev=False)
        self.fc_1 = ReluBactchNormTdnnLayer(input_dim,input_dim//ratio)
        self.fc_2 = TdnnAffine(input_dim//ratio, input_dim)

    def forward(self, inputs):
        """
        @inputs: a 3-dimensional tensor (a batch), including [samples-index, frames-dim-index, frames-index]
        """
        assert len(inputs.shape) == 3
        assert inputs.shape[1] == self.input_dim

        outputs = self.stats(inputs)
        outputs = self.fc_1(outputs)
        outputs = torch.sigmoid(self.fc_2(outputs))
        outputs = torch.mul(inputs,outputs)

        return outputs
