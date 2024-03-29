# Copyright (c) 2016, Konstantinos Kamnitsas
# All rights reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the BSD license. See the accompanying LICENSE file
# or read the terms at https://opensource.org/licenses/BSD-3-Clause.

from __future__ import absolute_import, print_function, division
import os

from deepmedicEN.frontEnd.session import Session
from deepmedicEN.frontEnd.configParsing.utils import getAbsPathEvenIfRelativeIsGiven
from deepmedicEN.frontEnd.configParsing.trainSessionParams import TrainSessionParameters
from deepmedicEN.frontEnd.sessHelpers import makeFoldersNeededForTrainingSession, handle_exception_tf_restore

from deepmedicEN.logging.utils import datetimeNowAsStr
from deepmedicEN.neuralnet.cnn3d import Cnn3d
from deepmedicEN.neuralnet.trainer import Trainer

from deepmedicEN.routines.training import do_training

import tensorflow as tf

class TrainSession(Session):
    
    def __init__(self, cfg):
        self._out_folder_models = None
        self._out_folder_preds = None
        self._out_folder_fms = None
        self._params = None # Compiled from cfg. Required for run()
        Session.__init__(self, cfg)
        
    def _make_sess_name(self):
        sess_name = TrainSessionParameters.getSessionName(  self._cfg[self._cfg.SESSION_NAME] )
        return sess_name
    
    def make_output_folders(self):
        [self._log_folder_abs,
         self._out_folder_models,
         self._out_folder_preds,
         self._out_folder_fms] = makeFoldersNeededForTrainingSession( self._main_out_folder_abs, self._sess_name )
    
    
    def _print_vars_in_collection(self, collection, coll_name="no_name"):
        self._log.print3("")
        self._log.print3("==== Printing variables of collection [" +str(coll_name) + "] ====")
        for entry in collection:
            self._log.print3(str(entry))
        self._log.print3("==== Done printing variables of collection. ====\n")
    
    
    def compile_session_params_from_cfg(self, *args):
        (model_params,) = args
        
        self._params = TrainSessionParameters(
                                    log = self._log,
                                    mainOutputAbsFolder = self._main_out_folder_abs,
                                    folderForSessionCnnModels = self._out_folder_models,
                                    folderForPredictionsVal = self._out_folder_preds,
                                    folderForFeaturesVal = self._out_folder_fms,
                                    num_classes = model_params.numberClasses,
                                    model_name = model_params.cnnModelName,
                                    cfg = self._cfg )
        
        self._log.print3("")
        self._log.print3("=============   NEW TRAINING SESSION     ==============\n")
        self._params.print_params()
        self._log.print3("=======================================================\n")
        
        return self._params
    
    
    def run_session(self, *args):
        (sess_device,
         model_params,
         reset_trainer) = args
        
        graphTf = tf.Graph()
        
        with graphTf.as_default():
            with graphTf.device(sess_device): # Explicit device assignment, throws an error if GPU is specified but not available.
                self._log.print3("=========== Making the CNN graph... ===============")
                cnn3d = Cnn3d()
                cnn3dT = Cnn3d()
                with tf.variable_scope("net"): 
                  
                    cnn3d.make_cnn_model( *model_params.get_args_for_arch() )
                    # I have now created the CNN graph. But not yet the Optimizer's graph.
            
            # No explicit device assignment for the rest. Because trained has piecewise_constant that is only on cpu, and so is saver.        
            with tf.variable_scope("trainer"):
                self._log.print3("=========== Building Trainer ===========\n")
                trainer = Trainer( *( self._params.get_args_for_trainer() + [cnn3d] ) )
                trainer.create_optimizer( *self._params.get_args_for_optimizer() ) # Trainer and net connect here.
                
            # The below should not create any new tf.variables.
            self._log.print3("=========== Compiling the Training Function ===========")
            self._log.print3("=======================================================\n")
            cnn3d.setup_ops_n_feeds_to_train( self._log,
                                              trainer.get_total_cost(),
                                              trainer.get_param_updates_wrt_total_cost() # list of ops
                                            )
            
            self._log.print3("=========== Compiling the Validation Function =========")
            cnn3d.setup_ops_n_feeds_to_val( self._log )
            
            self._log.print3("=========== Compiling the Testing Function ============")
            cnn3d.setup_ops_n_feeds_to_test( self._log,
                                             self._params.indices_fms_per_pathtype_per_layer_to_save ) # For validation with full segmentation
            
            # Create the savers
            saver_all = tf.train.Saver() # Will be used during training for saving everything.
            collection_vars_net = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope="net") # Alternative: tf.train.Saver([v for v in tf.all_variables() if v.name.startswith("net"])
            saver_net = tf.train.Saver( var_list = collection_vars_net ) # Used to load the net's parameters.
            collection_vars_trainer = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope="trainer")
            saver_trainer = tf.train.Saver( var_list = collection_vars_trainer ) # Used to load the trainer's parameters.
            
        # self._print_vars_in_collection(collection_vars_net, "net")
        # self._print_vars_in_collection(collection_vars_trainer, "trainer")
        
        with tf.Session( graph=graphTf, config=tf.ConfigProto(log_device_placement=False, device_count={'CPU':999, 'GPU':99}) ) as sessionTf:
            # Load or initialize parameters
            file_to_load_params_from = self._params.get_path_to_load_model_from()
            if file_to_load_params_from is not None: # Load params
                self._log.print3("=========== Loading parameters from specified saved model ===============")
                chkpt_fname = tf.train.latest_checkpoint( file_to_load_params_from ) if os.path.isdir( file_to_load_params_from ) else file_to_load_params_from
                self._log.print3("Loading checkpoint file:" + str(chkpt_fname))
                self._log.print3("Loading network parameters...")
                try:
                    saver_net.restore(sessionTf, chkpt_fname)
                    self._log.print3("Network parameters were loaded.")
                except Exception as e: handle_exception_tf_restore(self._log, e)
                
                if not reset_trainer:
                    self._log.print3("Loading trainer parameters...")
                    saver_trainer.restore(sessionTf, chkpt_fname)
                    self._log.print3("Trainer parameters were loaded.")
                else:
                    self._log.print3("Reset of trainer parameters was requested. Re-initializing them...")
                    tf.variables_initializer(var_list = collection_vars_trainer).run()
                    self._log.print3("Trainer parameters re-initialized.")
            else :
                self._log.print3("=========== Initializing network and trainer variables  ===============")
                # tf.variables_initializer(var_list = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES) ).run() # Initializes all.
                # Initialize separate as below, so that in case I miss a variable, I will get an error and I will know.
                tf.variables_initializer(var_list = collection_vars_net).run()
                tf.variables_initializer(var_list = collection_vars_trainer).run()
                self._log.print3("All variables were initialized.")
                
                filename_to_save_with = self._params.filepath_to_save_models + ".initial." + datetimeNowAsStr()
                self._log.print3("Saving the initial model at:" + str(filename_to_save_with))
                saver_all.save( sessionTf, filename_to_save_with+".model.ckpt", write_meta_graph=False )
                # tf.train.write_graph( graph_or_graph_def=sessionTf.graph.as_graph_def(), logdir="", name=filename_to_save_with+".graph.pb", as_text=False)
             
            self._log.print3("")
            self._log.print3("=======================================================")
            self._log.print3("============== Training the CNN model =================")
            self._log.print3("=======================================================\n")
            
            do_training( *( [sessionTf, saver_all, cnn3d, trainer] + self._params.get_args_for_train_routine() ) )
            
            # Save the trained model.
            filename_to_save_with = self._params.filepath_to_save_models + ".final." + datetimeNowAsStr()
            self._log.print3("Saving the final model at:" + str(filename_to_save_with))
            saver_all.save( sessionTf, filename_to_save_with+".model.ckpt", write_meta_graph=False )
            
            
        self._log.print3("\n=======================================================")
        self._log.print3("=========== Training session finished =================")
        self._log.print3("=======================================================")
        
