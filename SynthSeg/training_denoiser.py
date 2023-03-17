"""
If you use this code, please cite one of the SynthSeg papers:
https://github.com/BBillot/SynthSeg/blob/master/bibtex.bib

Copyright 2020 Benjamin Billot

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in
compliance with the License. You may obtain a copy of the License at
https://www.apache.org/licenses/LICENSE-2.0
Unless required by applicable law or agreed to in writing, software distributed under the License is
distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
implied. See the License for the specific language governing permissions and limitations under the
License.
"""


# python imports
import os
import numpy as np
import tensorflow as tf
from keras import models
from keras import layers as KL

# project imports
from SynthSeg import metrics_model as metrics
from SynthSeg.training import train_model
from SynthSeg.labels_to_image_model import get_shapes
from SynthSeg.training_supervised import build_model_inputs

# third-party imports
from ext.lab2im import utils, layers
from ext.neuron import models as nrn_models


def training(list_paths_input_labels,
             list_paths_target_labels,
             model_dir,
             input_segmentation_labels,
             target_segmentation_labels=None,
             subjects_prob=None,
             batchsize=1,
             output_shape=None,
             scaling_bounds=.2,
             rotation_bounds=15,
             shearing_bounds=.012,
             nonlin_std=3.,
             nonlin_scale=.04,
             prob_erosion_dilation=0.3,
             min_erosion_dilation=4,
             max_erosion_dilation=5,
             n_levels=5,
             nb_conv_per_level=2,
             conv_size=5,
             unet_feat_count=16,
             feat_multiplier=2,
             activation='elu',
             skip_n_concatenations=2,
             lr=1e-4,
             wl2_epochs=1,
             dice_epochs=50,
             steps_per_epoch=10000,
             checkpoint=None):
    """

    This function trains a UNet to segment MRI images with synthetic scans generated by sampling a GMM conditioned on
    label maps. We regroup the parameters in four categories: General, Augmentation, Architecture, Training.

    # IMPORTANT !!!
    # Each time we provide a parameter with separate values for each axis (e.g. with a numpy array or a sequence),
    # these values refer to the RAS axes.

    :param: list_paths_input_labels: list of all the paths of the input label maps. These correspond to "noisy"
    segmentations that the denoiser will be trained to correct.
    :param list_paths_target_labels: list of all the paths of the output label maps. Must have the same order as
    list_paths_input_labels. These are the target label maps that the network will learn to produce given the "noisy"
    input label maps.
    :param model_dir: path of a directory where the models will be saved during training.
    :param input_segmentation_labels: list of all the label values present in the input label maps.
    :param target_segmentation_labels: list of all the label values present in the output label maps. By default (None)
    this will be taken to be the same as input_segmentation_labels.

    # ----------------------------------------------- General parameters -----------------------------------------------
    # label maps parameters
    :param subjects_prob: (optional) relative order of importance (doesn't have to be probabilistic), with which to pick
    the provided label maps at each minibatch. Can be a sequence, a 1D numpy array, or the path to such an array, and it
    must be as long as path_label_maps. By default, all label maps are chosen with the same importance.

    # output-related parameters
    :param batchsize: (optional) number of images to generate per mini-batch. Default is 1.
    :param output_shape: (optional) desired shape of the output image, obtained by randomly cropping the generated image
    Can be an integer (same size in all dimensions), a sequence, a 1d numpy array, or the path to a 1d numpy array.
    Default is None, where no cropping is performed.

    # --------------------------------------------- Augmentation parameters --------------------------------------------
    # spatial deformation parameters
    :param scaling_bounds: (optional) if apply_linear_trans is True, the scaling factor for each dimension is
    sampled from a uniform distribution of predefined bounds. Can either be:
    1) a number, in which case the scaling factor is independently sampled from the uniform distribution of bounds
    (1-scaling_bounds, 1+scaling_bounds) for each dimension.
    2) the path to a numpy array of shape (2, n_dims), in which case the scaling factor in dimension i is sampled from
    the uniform distribution of bounds (scaling_bounds[0, i], scaling_bounds[1, i]) for the i-th dimension.
    3) False, in which case scaling is completely turned off.
    Default is scaling_bounds = 0.2 (case 1)
    :param rotation_bounds: (optional) same as scaling bounds but for the rotation angle, except that for case 1 the
    bounds are centred on 0 rather than 1, i.e. (0+rotation_bounds[i], 0-rotation_bounds[i]).
    Default is rotation_bounds = 15.
    :param shearing_bounds: (optional) same as scaling bounds. Default is shearing_bounds = 0.012.
    :param nonlin_std: (optional) Standard deviation of the normal distribution from which we sample the first
    tensor for synthesising the deformation field. Set to 0 to completely deactivate elastic deformation.
    :param nonlin_scale: (optional) Ratio between the size of the input label maps and the size of the sampled
    tensor for synthesising the elastic deformation field.
    
    # degradation of the input labels
    :param prob_erosion_dilation: (optional) probability with which to degrade the input label maps with erosion or 
    dilation. If 0, then no erosion/dilation is applied to the label maps given as inputs to the network.
    :param min_erosion_dilation: (optional) when prob_erosion_dilation is not zero, erosion and dilation of random
    coefficients are applied. Set the minimum erosion/dilation coefficient here.
    :param max_erosion_dilation: (optional) Set the maximum erosion/dilation coefficient here.

    # ------------------------------------------ UNet architecture parameters ------------------------------------------
    :param n_levels: (optional) number of level for the Unet. Default is 5.
    :param nb_conv_per_level: (optional) number of convolutional layers per level. Default is 2.
    :param conv_size: (optional) size of the convolution kernels. Default is 2.
    :param unet_feat_count: (optional) number of feature for the first layer of the UNet. Default is 24.
    :param feat_multiplier: (optional) multiply the number of feature by this number at each new level. Default is 2.
    :param activation: (optional) activation function. Can be 'elu', 'relu'.
    :param skip_n_concatenations: (optional) number of levels for which to remove the traditional skip connections of
    the UNet architecture. default is zero, which corresponds to the classic UNet architecture. Example:
    If skip_n_concatenations = 2, then we will remove the concatenation link between the two top levels of the UNet.

    # ----------------------------------------------- Training parameters ----------------------------------------------
    :param lr: (optional) learning rate for the training. Default is 1e-4
    :param wl2_epochs: (optional) number of epochs for which the network (except the soft-max layer) is trained with L2
    norm loss function. Default is 1.
    :param dice_epochs: (optional) number of epochs with the soft Dice loss function. Default is 50.
    :param steps_per_epoch: (optional) number of steps per epoch. Default is 10000. Since no online validation is
    possible, this is equivalent to the frequency at which the models are saved.
    :param checkpoint: (optional) path of an already saved model to load before starting the training.
    """

    # check epochs
    assert (wl2_epochs > 0) | (dice_epochs > 0), \
        'either wl2_epochs or dice_epochs must be positive, had {0} and {1}'.format(wl2_epochs, dice_epochs)

    # prepare data files
    input_label_list, _ = utils.get_list_labels(label_list=input_segmentation_labels)
    if target_segmentation_labels is None:
        target_label_list = input_label_list
    else:
        target_label_list, _ = utils.get_list_labels(label_list=target_segmentation_labels)
    n_labels = np.size(target_label_list)

    # create augmentation model
    labels_shape, _, _, _, _, _ = utils.get_volume_info(list_paths_input_labels[0], aff_ref=np.eye(4))
    augmentation_model = build_augmentation_model(labels_shape,
                                                  input_label_list,
                                                  crop_shape=output_shape,
                                                  output_div_by_n=2 ** n_levels,
                                                  scaling_bounds=scaling_bounds,
                                                  rotation_bounds=rotation_bounds,
                                                  shearing_bounds=shearing_bounds,
                                                  nonlin_std=nonlin_std,
                                                  nonlin_scale=nonlin_scale,
                                                  prob_erosion_dilation=prob_erosion_dilation,
                                                  min_erosion_dilation=min_erosion_dilation,
                                                  max_erosion_dilation=max_erosion_dilation)
    unet_input_shape = augmentation_model.output[0].get_shape().as_list()[1:]

    # prepare the segmentation model
    l2l_model = nrn_models.unet(input_model=augmentation_model,
                                input_shape=unet_input_shape,
                                nb_labels=n_labels,
                                nb_levels=n_levels,
                                nb_conv_per_level=nb_conv_per_level,
                                conv_size=conv_size,
                                nb_features=unet_feat_count,
                                feat_mult=feat_multiplier,
                                activation=activation,
                                batch_norm=-1,
                                skip_n_concatenations=skip_n_concatenations,
                                name='l2l')

    # input generator
    model_inputs = build_model_inputs(path_inputs=list_paths_input_labels,
                                      path_outputs=list_paths_target_labels,
                                      batchsize=batchsize,
                                      subjects_prob=subjects_prob,
                                      dtype_input='int32')
    input_generator = utils.build_training_generator(model_inputs, batchsize)

    # pre-training with weighted L2, input is fit to the softmax rather than the probabilities
    if wl2_epochs > 0:
        wl2_model = models.Model(l2l_model.inputs, [l2l_model.get_layer('l2l_likelihood').output])
        wl2_model = metrics.metrics_model(wl2_model, target_label_list, 'wl2')
        train_model(wl2_model, input_generator, lr, wl2_epochs, steps_per_epoch, model_dir, 'wl2', checkpoint)
        checkpoint = os.path.join(model_dir, 'wl2_%03d.h5' % wl2_epochs)

    # fine-tuning with dice metric
    dice_model = metrics.metrics_model(l2l_model, target_label_list, 'dice')
    train_model(dice_model, input_generator, lr, dice_epochs, steps_per_epoch, model_dir, 'dice', checkpoint)


def build_augmentation_model(labels_shape,
                             segmentation_labels,
                             crop_shape=None,
                             output_div_by_n=None,
                             scaling_bounds=0.15,
                             rotation_bounds=15,
                             shearing_bounds=0.012,
                             translation_bounds=False,
                             nonlin_std=3.,
                             nonlin_scale=.0625,
                             prob_erosion_dilation=0.3,
                             min_erosion_dilation=4,
                             max_erosion_dilation=7):

    # reformat resolutions and get shapes
    labels_shape = utils.reformat_to_list(labels_shape)
    n_dims, _ = utils.get_dims(labels_shape)
    n_labels = len(segmentation_labels)

    # get shapes
    crop_shape, _ = get_shapes(labels_shape, crop_shape, np.array([1]*n_dims), np.array([1]*n_dims), output_div_by_n)

    # define model inputs
    net_input = KL.Input(shape=labels_shape + [1], name='l2l_noisy_labels_input', dtype='int32')
    target_input = KL.Input(shape=labels_shape + [1], name='l2l_target_input', dtype='int32')

    # deform labels
    noisy_labels, target = layers.RandomSpatialDeformation(scaling_bounds=scaling_bounds,
                                                           rotation_bounds=rotation_bounds,
                                                           shearing_bounds=shearing_bounds,
                                                           translation_bounds=translation_bounds,
                                                           nonlin_std=nonlin_std,
                                                           nonlin_scale=nonlin_scale,
                                                           inter_method='nearest')([net_input, target_input])

    # cropping
    if crop_shape != labels_shape:
        noisy_labels, target = layers.RandomCrop(crop_shape)([noisy_labels, target])

    # random erosion
    if prob_erosion_dilation > 0:
        noisy_labels = layers.RandomDilationErosion(min_erosion_dilation,
                                                    max_erosion_dilation,
                                                    prob=prob_erosion_dilation)(noisy_labels)

    # convert input labels (i.e. noisy_labels) to [0, ... N-1] and make them one-hot
    noisy_labels = layers.ConvertLabels(np.unique(segmentation_labels))(noisy_labels)
    target = KL.Lambda(lambda x: tf.cast(x[..., 0], 'int32'), name='labels_out')(target)
    noisy_labels = KL.Lambda(lambda x: tf.one_hot(x[0][..., 0], depth=n_labels),
                             name='noisy_labels_out')([noisy_labels, target])

    # build model and return
    brain_model = models.Model(inputs=[net_input, target_input], outputs=[noisy_labels, target])
    return brain_model
