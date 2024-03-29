# Copyright (c) 2016, Konstantinos Kamnitsas
# All rights reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the BSD license. See the accompanying LICENSE file
# or read the terms at https://opensource.org/licenses/BSD-3-Clause.

from __future__ import absolute_import, print_function, division

import os
import sys
import time
import numpy as np
import math
import random
import traceback
import multiprocessing
import signal
import collections

from deepmedicO.dataManagement.io import loadVolume
from deepmedicO.dataManagement.preprocessing import calculateTheZeroIntensityOf3dImage, padCnnInputs
from deepmedicO.neuralnet.pathwayTypes import PathwayTypes as pt
from deepmedicO.dataManagement.augmentSample import augment_sample
from deepmedicO.dataManagement.augmentImage import augment_images_of_case
# Order of calls:
# getSampledDataAndLabelsForSubepoch
#    get_random_subjects_to_train_subep
#    get_n_samples_per_subj_in_subep
#    load_subj_and_get_samples
#        load_imgs_of_subject
#        sample_coords_of_segments
#        extractSegmentGivenSliceCoords
#            getImagePartFromSubsampledImageForTraining
#    shuffleSegments

# Main sampling process during training. Executed in parallel while training on a batch on GPU.
# Called from training.do_training()
def getSampledDataAndLabelsForSubepoch( log,
                                        train_or_val,
                                        num_parallel_proc,
                                        run_input_checks,
                                        cnn3d,
                                        max_n_cases_per_subep,
                                        n_samples_per_subep,
                                        sampling_type,
                                        # Paths to input files
                                        paths_per_chan_per_subj,
                                        paths_to_lbls_per_subj,
                                        paths_to_masks_per_subj,
                                        paths_to_wmaps_per_sampl_cat_per_subj,
                                        # Preprocessing & Augmentation
                                        pad_input_imgs,
                                        augm_img_prms,
                                        augm_sample_prms
                                        ):
    # Returns: channs_of_samples_arr_per_path - List of arrays [N_samples, Channs, R,C,Z], one per pathway.
    #          lbls_predicted_part_of_samples_arr - Array of shape: [N_samples, R_out, C_out, Z_out)
    
    id_str = "[SAMPLER-TR|PID:"+str(os.getpid())+"]" if train_or_val == "train" else "[SAMPLER-VAL|PID:"+str(os.getpid())+"]"
    start_time_sampling = time.time()
    training_or_validation_str = "Training" if train_or_val == "train" else "Validation"
    
    log.print3(id_str+" :=:=:=:=:=:=: Starting to sample for next [" + training_or_validation_str + "]... :=:=:=:=:=:=:")
    
    total_number_of_subjects = len(paths_per_chan_per_subj)
    inds_of_subjects_for_subep = get_random_subjects_to_train_subep( total_number_of_subjects = total_number_of_subjects,
                                                                     max_subjects_on_gpu_for_subepoch = max_n_cases_per_subep,
                                                                     get_max_subjects_for_gpu_even_if_total_less = False )
    log.print3(id_str+" Out of [" + str(total_number_of_subjects) + "] subjects given for [" + training_or_validation_str + "], "+
               "we will sample from maximum [" + str(max_n_cases_per_subep) + "] per subepoch.")
    log.print3(id_str+" Shuffled indices of subjects that were randomly chosen: "+str(inds_of_subjects_for_subep))
    
    # List, with [numberOfPathwaysThatTakeInput] sublists. Each sublist is list of [partImagesLoadedPerSubepoch] arrays [channels, R,C,Z].
    channs_of_samples_per_path_for_subep = [ [] for i in range(cnn3d.getNumPathwaysThatRequireInput()) ]
    lbls_predicted_part_of_samples_for_subep = [] # Labels only for the central/predicted part of segments.
    n_subjects_for_subep = len(inds_of_subjects_for_subep) #Can be different than max_n_cases_per_subep, cause of available images number.
    
    # Get how many samples I should get from each subject.
    n_samples_per_subj = get_n_samples_per_subj_in_subep( n_samples_per_subep, n_subjects_for_subep )
    
    args_sampling_job = [log,
                        train_or_val,
                        run_input_checks,
                        cnn3d,
                        sampling_type,
                        paths_per_chan_per_subj,
                        paths_to_lbls_per_subj,
                        paths_to_masks_per_subj,
                        paths_to_wmaps_per_sampl_cat_per_subj,
                        # Pre-processing:
                        pad_input_imgs,
                        augm_img_prms,
                        augm_sample_prms,
                        
                        n_subjects_for_subep,
                        inds_of_subjects_for_subep,
                        n_samples_per_subj ]
    
    log.print3(id_str+" Will sample from [" + str(n_subjects_for_subep) + "] subjects for next " + training_or_validation_str + "...")
    
    jobs_inds_to_do = list(range(n_subjects_for_subep)) # One job per subject.
    
    if num_parallel_proc <= 0: # Sequentially
        for job_i in jobs_inds_to_do :
            (channs_of_samples_from_job_per_path,
            lbls_predicted_part_of_samples_from_job) = load_subj_and_get_samples( *( [job_i]+args_sampling_job ) )
            for pathway_i in range(cnn3d.getNumPathwaysThatRequireInput()) :
                channs_of_samples_per_path_for_subep[pathway_i] += channs_of_samples_from_job_per_path[pathway_i] # concat does not copy.
            lbls_predicted_part_of_samples_for_subep += lbls_predicted_part_of_samples_from_job # concat does not copy.

    else: # Parallelize sampling from each subject
        while len(jobs_inds_to_do) > 0: # While jobs remain.
            jobs = collections.OrderedDict()
            
            log.print3(id_str+" ******* Spawning children processes to sample from [" + str(len(jobs_inds_to_do)) + "] subjects*******")
            log.print3(id_str+" MULTIPROC: Number of CPUs detected: " + str(multiprocessing.cpu_count()) + ". Requested to use max: [" + str(num_parallel_proc) + "]")
            num_workers = min(num_parallel_proc, multiprocessing.cpu_count())
            log.print3(id_str+" MULTIPROC: Spawning [" + str(num_workers) + "] processes to load data and sample.")
            worker_pool = multiprocessing.Pool(processes=num_workers, initializer=init_sampling_proc) 

            try: # Stacktrace in multiprocessing: https://jichu4n.com/posts/python-multiprocessing-and-exceptions/
                for job_i in jobs_inds_to_do: # submit jobs
                    jobs[job_i] = worker_pool.apply_async( load_subj_and_get_samples, ( [job_i]+args_sampling_job ) )
                
                for job_i in list(jobs_inds_to_do): # copy with list(...), so that this loops normally even if something is removed from list.
                    try:
                        (channs_of_samples_from_job_per_path,
                        lbls_predicted_part_of_samples_from_job) = jobs[job_i].get(timeout=30) # timeout in case process for some reason never started (happens in py3)
                        for pathway_i in range(cnn3d.getNumPathwaysThatRequireInput()) :
                            channs_of_samples_per_path_for_subep[pathway_i] += channs_of_samples_from_job_per_path[pathway_i] # concat does not copy.
                        lbls_predicted_part_of_samples_for_subep += lbls_predicted_part_of_samples_from_job # concat does not copy.
                        jobs_inds_to_do.remove(job_i)
                    except multiprocessing.TimeoutError as e:
                        log.print3(id_str+"\n\n WARN: MULTIPROC: Caught TimeoutError when getting results of job [" + str(job_i) + "].\n" +
                                   " WARN: MULTIPROC: Will resubmit job [" + str(job_i) + "].\n")
                        if num_workers == 1:
                            break # If this 1 worker got stuck, every job will wait timeout. Slow. Recreate pool.
            
            except (Exception, KeyboardInterrupt) as e:
                log.print3(id_str+"\n\n ERROR: Caught exception in getSampledDataAndLabelsForSubepoch(): "+str(e)+"\n")
                log.print3( traceback.format_exc() )
                worker_pool.terminate()
                worker_pool.join() # Will wait. A KeybInt will kill this (py3)
                raise e
            except: # Catches everything, even a sys.exit(1) exception.
                log.print3(id_str+"\n\n ERROR: Unexpected error in getSampledDataAndLabelsForSubepoch(). System info: ", sys.exc_info()[0])
                worker_pool.terminate()
                worker_pool.join()
                raise Exception("Unexpected error.")
            else: # Nothing went wrong
                worker_pool.terminate() # # Needed in case any processes are hunging. worker_pool.close() does not solve this.
                worker_pool.join()
               
        
    # Got all samples for subepoch. Now shuffle them, together segments and their labels.
    (channs_of_samples_per_path_for_subep,
    lbls_predicted_part_of_samples_for_subep) = shuffleSegments(channs_of_samples_per_path_for_subep,
                                                                 lbls_predicted_part_of_samples_for_subep )
    end_time_sampling = time.time()
    log.print3(id_str+" TIMING: Sampling for next [" + training_or_validation_str + "] lasted: {0:.1f}".format(end_time_sampling-start_time_sampling)+" secs.")
    
    log.print3(id_str+" :=:=:=:=:=:=: Finished sampling for next [" + training_or_validation_str + "] :=:=:=:=:=:=:")
    
    channs_of_samples_arr_per_path = [ np.asarray(channs_of_samples_for_path, dtype="float32") for channs_of_samples_for_path in channs_of_samples_per_path_for_subep ]
    

    lbls_predicted_part_of_samples_arr = np.asarray(lbls_predicted_part_of_samples_for_subep, dtype="int32") # Could be int16 to save RAM?
    
    return (channs_of_samples_arr_per_path, lbls_predicted_part_of_samples_arr)
    
    
    
def init_sampling_proc():
    # This will make child-processes ignore the KeyboardInterupt (sigInt). Parent will handle it.
    # See: http://stackoverflow.com/questions/11312525/catch-ctrlc-sigint-and-exit-multiprocesses-gracefully-in-python/35134329#35134329 
    signal.signal(signal.SIGINT, signal.SIG_IGN) 


    
def get_random_subjects_to_train_subep( total_number_of_subjects, 
                                        max_subjects_on_gpu_for_subepoch, 
                                        get_max_subjects_for_gpu_even_if_total_less=False ):
    # Returns: list of indices
    subjects_indices = list(range(total_number_of_subjects)) #list() for python3 compatibility, as range cannot get assignment in shuffle()
    random_order_chosen_subjects=[]
    random.shuffle(subjects_indices) #does it in place. Now they are shuffled
    
    if max_subjects_on_gpu_for_subepoch>=total_number_of_subjects:
        random_order_chosen_subjects += subjects_indices
        
        if get_max_subjects_for_gpu_even_if_total_less : #This is if I want to have a certain amount on GPU, even if total subjects are less.
            while (len(random_order_chosen_subjects)<max_subjects_on_gpu_for_subepoch):
                random.shuffle(subjects_indices)
                number_of_extra_subjects_to_get_to_fill_gpu = min(max_subjects_on_gpu_for_subepoch - len(random_order_chosen_subjects), total_number_of_subjects)
                random_order_chosen_subjects += (subjects_indices[:number_of_extra_subjects_to_get_to_fill_gpu])
            assert len(random_order_chosen_subjects) != max_subjects_on_gpu_for_subepoch
    else:
        random_order_chosen_subjects += subjects_indices[:max_subjects_on_gpu_for_subepoch]
        
    return random_order_chosen_subjects


def get_n_samples_per_subj_in_subep(n_samples, n_subjects):
    # Distribute samples of each cat to subjects.
    n_samples_per_subj = np.ones([n_subjects] , dtype="int32") * (n_samples // n_subjects)
    n_undistributed_samples = n_samples % n_subjects
    # Distribute samples that were left by inexact division.
    for idx in range(n_undistributed_samples):
        n_samples_per_subj[random.randint(0, n_subjects - 1)] += 1
    return n_samples_per_subj

    
def load_subj_and_get_samples(job_i,
                              log,
                              train_or_val,
                              run_input_checks,
                              cnn3d,
                              sampling_type,
                              paths_per_chan_per_subj,
                              paths_to_lbls_per_subj,
                              paths_to_masks_per_subj,
                              paths_to_wmaps_per_sampl_cat_per_subj,
                              # Pre-processing:
                              pad_input_imgs,
                              augm_img_prms,
                              augm_sample_prms,
                              
                              n_subjects_for_subep,
                              inds_of_subjects_for_subep,
                              n_samples_per_subj
                              ):
    # paths_per_chan_per_subj: [ [ for channel-0 [ one path per subj ] ], ..., [ for channel-n  [ one path per subj ] ] ]
    # n_samples_per_cat_per_subj: np arr, shape [num sampling categories, num subjects in subepoch]
    # returns: ( channs_of_samples_per_path, lbls_predicted_part_of_samples )
    id_str = "[JOB:"+str(job_i)+"|PID:"+str(os.getpid())+"]"
    log.print3(id_str+" Load & sample from subject of index (in user's list): " + str(inds_of_subjects_for_subep[job_i]) +\
               " (Job #" + str(job_i) + "/" +str(n_subjects_for_subep)+")")
    
    # List, with [numberOfPathwaysThatTakeInput] sublists. Each sublist is list of [partImagesLoadedPerSubepoch] arrays [channels, R,C,Z].
    channs_of_samples_per_path = [ [] for i in range(cnn3d.getNumPathwaysThatRequireInput()) ]
    lbls_predicted_part_of_samples = [] # Labels only for the central/predicted part of segments.
    
    dims_highres_segment = cnn3d.pathways[0].getShapeOfInput(train_or_val)[2:]
    
    (channels, # nparray [channels,dim0,dim1,dim2]
    gt_lbl_img,
    roi_mask,
    weightmaps_to_sample_per_cat,
    pad_added_prepost_each_axis
    ) = load_imgs_of_subject(log,
                             job_i,
                             train_or_val,
                             run_input_checks,
                             inds_of_subjects_for_subep[job_i],
                             paths_per_chan_per_subj,
                             paths_to_lbls_per_subj, 
                             paths_to_wmaps_per_sampl_cat_per_subj, # Placeholder in testing.
                             paths_to_masks_per_subj,
                             cnn3d.num_classes,
                             # Preprocessing
                             pad_input_imgs,
                             cnn3d.recFieldCnn, # used if pad_input_imgs
                             dims_highres_segment) # used if pad_input_imgs.
    
    # Augment at image level:
    time_augm_0 = time.time()
    (channels,
    gt_lbl_img,
    roi_mask,
    weightmaps_to_sample_per_cat) = augment_images_of_case(channels,
                                                           gt_lbl_img,
                                                           roi_mask,
                                                           weightmaps_to_sample_per_cat,
                                                           augm_img_prms)
    time_augm_img = time.time()-time_augm_0
    
    # Sampling of segments (sub-volumes) from an image.
    dims_of_scan = channels[0].shape
    sampling_maps_per_cat = sampling_type.logicDecidingSamplingMapsPerCategory(
                                                weightmaps_to_sample_per_cat,
                                                gt_lbl_img,
                                                roi_mask,
                                                dims_of_scan)
    
    # Get number of samples per sampling-category for the specific subject (class, foregr/backgr, etc)
    (n_samples_per_cat, valid_cats) = sampling_type.distribute_n_samples_to_categs(n_samples_per_subj[job_i], sampling_maps_per_cat)
    
    str_samples_per_cat = " Got samples per category: "
    for cat_i in range( sampling_type.getNumberOfCategoriesToSample() ) :
        cat_string = sampling_type.getStringsPerCategoryToSample()[cat_i]
        n_samples_for_cat = n_samples_per_cat[cat_i]
        sampling_map = sampling_maps_per_cat[cat_i]
        # Check if the class is valid for sampling. Invalid if eg there is no such class in the subject's manual segmentation.
        if not valid_cats[cat_i] :
            log.print3(id_str+" WARN: Invalid sampling category! Sampling map just zeros! No [" + cat_string + "] samples from this subject!")
            assert n_samples_for_cat == 0
            continue
        
        coords_of_samples = sample_coords_of_segments(log,
                                            job_i,
                                            n_samples_for_cat,
                                            dims_highres_segment,
                                            dims_of_scan,
                                            sampling_map)
        str_samples_per_cat += "[" + cat_string + ": " + str(len(coords_of_samples[0][0])) + "/" + str(n_samples_for_cat) + "] "
        
        # Use the just sampled coordinates of slices to actually extract the segments (data) from the subject's images. 
        for image_part_i in range(len(coords_of_samples[0][0])) :
            coord_center = coords_of_samples[0][:,image_part_i]
            
            (channs_of_sample_per_path,
            lbls_predicted_part_of_sample # used to be gtLabelsForThisImagePart, before extracting only for the central voxels.
            ) = extractSegmentGivenSliceCoords(train_or_val,
                                               cnn3d,
                                               coord_center,
                                               channels,
                                               gt_lbl_img)
            
            # Augmentation of segments
            (channs_of_sample_per_path,
            lbls_predicted_part_of_sample) = augment_sample(channs_of_sample_per_path,
                                                            lbls_predicted_part_of_sample,
                                                            augm_sample_prms)
            
            for pathway_i in range(cnn3d.getNumPathwaysThatRequireInput()) :
                channs_of_samples_per_path[pathway_i].append( channs_of_sample_per_path[pathway_i] )
            lbls_predicted_part_of_samples.append( lbls_predicted_part_of_sample )
        
    log.print3(id_str + str_samples_per_cat + ". Seconds augmenting [Image: {0:.1f}".format(time_augm_img)+"]")
    return (channs_of_samples_per_path, lbls_predicted_part_of_samples)



# roi_mask_filename and roiMinusLesion_mask_filename can be passed "no". In this case, the corresponding return result is nothing.
# This is so because: the do_training() function only needs the roiMinusLesion_mask, whereas the do_testing() only needs the roi_mask.        
def load_imgs_of_subject(log,
                         job_i, # None in testing.
                         train_val_or_test,
                         run_input_checks,
                         subj_i,
                         paths_per_chan_per_subj,
                         paths_to_lbls_per_subj,
                         paths_to_wmaps_per_sampl_cat_per_subj, # Placeholder in testing.
                         paths_to_masks_per_subj,
                         num_classes,
                         # Preprocessing
                         pad_input_imgs,
                         cnnReceptiveField, # only used if pad_input_imgs
                         dims_highres_segment
                         ):
    # paths_per_chan_per_subj: List of lists. One sublist per case. Each should contain...
    # ... as many elements(strings-filenamePaths) as numberOfChannels, pointing to (nii) channels of this case.
    id_str = "[JOB:"+str(job_i)+"|PID:"+str(os.getpid())+"]" if job_i is not None else "" # is None in testing.
    
    if subj_i >= len(paths_per_chan_per_subj) :
        raise ValueError(id_str+" The argument 'subj_i' given is greater than the filenames given for the .nii folders!")
    
    log.print3(id_str+" Loading subject with 1st channel at: "+str(paths_per_chan_per_subj[subj_i][0]))
    
    numberOfNormalScaleChannels = len(paths_per_chan_per_subj[0])

    pad_added_prepost_each_axis = ((0,0), (0,0), (0,0)) # Padding added before and after each axis.
    
    if paths_to_masks_per_subj is not None :
        fullFilenamePathOfRoiMask = paths_to_masks_per_subj[subj_i]
        roi_mask = loadVolume(fullFilenamePathOfRoiMask)
        
        (roi_mask, pad_added_prepost_each_axis) = padCnnInputs(roi_mask, cnnReceptiveField, dims_highres_segment) if pad_input_imgs else [roi_mask, pad_added_prepost_each_axis]
    else :
        roi_mask = None
        
    #Load the channels of the patient.
    niiDimensions = None
    channels = None
    
    for channel_i in range(numberOfNormalScaleChannels):
        fullFilenamePathOfChannel = paths_per_chan_per_subj[subj_i][channel_i]
        if fullFilenamePathOfChannel != "-" : #normal case, filepath was given.
            channelData = loadVolume(fullFilenamePathOfChannel)
                
            (channelData, pad_added_prepost_each_axis) = padCnnInputs(channelData, cnnReceptiveField, dims_highres_segment) if pad_input_imgs else [channelData, pad_added_prepost_each_axis]
            
            if channels is None :
                #Initialize the array in which all the channels for the patient will be placed.
                niiDimensions = list(channelData.shape)
                channels = np.zeros( (numberOfNormalScaleChannels, niiDimensions[0], niiDimensions[1], niiDimensions[2]))
                
            channels[channel_i] = channelData
        else : # "-" was given in the config-listing file. Do Min-fill!
            log.print3(id_str+" WARN: No modality #"+str(channel_i)+" given. Will make input channel full of zeros.")
            channels[channel_i] = -4.0
            
        
    #Load the class labels.
    if paths_to_lbls_per_subj is not None :
        fullFilenamePathOfGtLabels = paths_to_lbls_per_subj[subj_i]
        imageGtLabels = loadVolume(fullFilenamePathOfGtLabels)
        
        if imageGtLabels.dtype.kind not in ['i','u']:
            log.print3(id_str+" WARN: Loaded labels are dtype ["+str(imageGtLabels.dtype)+"]. Rounding and casting them to int!")
            imageGtLabels = np.rint(imageGtLabels).astype("int32")
            
        if run_input_checks:
            check_gt_vs_num_classes(log, imageGtLabels, num_classes)

        (imageGtLabels, pad_added_prepost_each_axis) = padCnnInputs(imageGtLabels, cnnReceptiveField, dims_highres_segment) if pad_input_imgs else [imageGtLabels, pad_added_prepost_each_axis]
    else : 
        imageGtLabels = None #For validation and testing
        
    if train_val_or_test != "test" and paths_to_wmaps_per_sampl_cat_per_subj is not None : # May be provided only for training.
        n_sampl_categs = len(paths_to_wmaps_per_sampl_cat_per_subj)
        weightmaps_to_sample_per_cat = np.zeros( [n_sampl_categs] + list(channels[0].shape), dtype="float32" ) 
        for cat_i in range( n_sampl_categs ) :
            filepathsToTheWeightMapsOfAllPatientsForThisCategory = paths_to_wmaps_per_sampl_cat_per_subj[cat_i]
            filepathToTheWeightMapOfThisPatientForThisCategory = filepathsToTheWeightMapsOfAllPatientsForThisCategory[subj_i]
            weightedMapForThisCatData = loadVolume(filepathToTheWeightMapOfThisPatientForThisCategory)
            assert np.all(weightedMapForThisCatData >= 0)
            
            (weightedMapForThisCatData, pad_added_prepost_each_axis) = padCnnInputs(weightedMapForThisCatData, cnnReceptiveField, dims_highres_segment) if pad_input_imgs else [weightedMapForThisCatData, pad_added_prepost_each_axis]
            
            weightmaps_to_sample_per_cat[cat_i] = weightedMapForThisCatData
    else :
        weightmaps_to_sample_per_cat = None
    
    return (channels, imageGtLabels, roi_mask, weightmaps_to_sample_per_cat, pad_added_prepost_each_axis)



#made for 3d
def sample_coords_of_segments(  log,
                                job_i,
                                numOfSegmentsToExtractForThisSubject,
                                dimsOfSegmentRcz,
                                dims_of_scan,
                                weightMapToSampleFrom ) :
    """
    This function returns the coordinates (index) of the "central" voxel of sampled image parts (1voxel to the left if even part-dimension).
    It also returns the indices of the image parts, left and right indices, INCLUSIVE BOTH SIDES.
    
    Return value: [ rcz-coordsOfCentralVoxelsOfPartsSampled, rcz-sliceCoordsOfImagePartsSampled ]
    > coordsOfCentralVoxelsOfPartsSampled : an array with shape: 3(rcz) x numOfSegmentsToExtractForThisSubject. 
        Example: [ rCoordsForCentralVoxelOfEachPart, cCoordsForCentralVoxelOfEachPart, zCoordsForCentralVoxelOfEachPart ]
        >> r/c/z-CoordsForCentralVoxelOfEachPart : A 1-dim array with numOfSegmentsToExtractForThisSubject, that holds the r-index within the image of each sampled part.
    > sliceCoordsOfImagePartsSampled : 3(rcz) x NumberOfImagePartSamples x 2. The last dimension has [0] for the lower boundary of the slice, and [1] for the higher boundary. INCLUSIVE BOTH SIDES.
        Example: [ r-sliceCoordsOfImagePart, c-sliceCoordsOfImagePart, z-sliceCoordsOfImagePart ]
    """
    id_str = "[JOB:"+str(job_i)+"|PID:"+str(os.getpid())+"]"
    # Check if the weight map is fully-zeros. In this case, return no element.
    # Note: Currently, the caller function is checking this case already and does not let this being called. Which is still fine.
    if np.sum(weightMapToSampleFrom) == 0 :
        log.print3(id_str+" WARN: The sampling mask/map was found just zeros! No image parts were sampled for this subject!")
        return [ [[],[],[]], [[],[],[]] ]
    
    imagePartsSampled = []
    
    #Now out of these, I need to randomly select one, which will be an ImagePart's central voxel.
    #But I need to be CAREFUL and get one that IS NOT closer to the image boundaries than the dimensions of the ImagePart permit.
    
    #I look for lesions that are not closer to the image boundaries than the ImagePart dimensions allow.
    #KernelDim is always odd. BUT ImagePart dimensions can be odd or even.
    #If odd, ok, floor(dim/2) from central.
    #If even, dim/2-1 voxels towards the begining of the axis and dim/2 towards the end. Ie, "central" imagePart voxel is 1 closer to begining.
    #BTW imagePartDim takes kernel into account (ie if I want 9^3 voxels classified per imagePart with kernel 5x5, I want 13 dim ImagePart)
    
    halfImagePartBoundaries = np.zeros( (len(dimsOfSegmentRcz), 2) , dtype='int32') #dim1: 1 row per r,c,z. Dim2: left/right width not to sample from (=half segment).
    
    #The below starts all zero. Will be Multiplied by other true-false arrays expressing if the relevant voxels are within boundaries.
    #In the end, the final vector will be true only for the indices of lesions that are within all boundaries.
    booleanNpArray_voxelsToCentraliseImPartsWithinBoundaries = np.zeros(weightMapToSampleFrom.shape, dtype="int32")
    
    # This loop leads to booleanNpArray_voxelsToCentraliseImPartsWithinBoundaries to be true for the indices ...
    # ...that allow getting an imagePart CENTERED on them, and be safely within image boundaries. Note that ...
    # ... if the imagePart is of even dimension, the "central" voxel is one voxel to the left.
    for rcz_i in range( len(dimsOfSegmentRcz) ) :
        if dimsOfSegmentRcz[rcz_i]%2 == 0: #even
            dimensionDividedByTwo = dimsOfSegmentRcz[rcz_i]//2
            halfImagePartBoundaries[rcz_i] = [dimensionDividedByTwo - 1, dimensionDividedByTwo] #central of ImagePart is 1 vox closer to begining of axes.
        else: #odd
            dimensionDividedByTwoFloor = math.floor(dimsOfSegmentRcz[rcz_i]//2) #eg 5/2 = 2, with the 3rd voxel being the "central"
            halfImagePartBoundaries[rcz_i] = [dimensionDividedByTwoFloor, dimensionDividedByTwoFloor] 
    #used to be [halfImagePartBoundaries[0][0]: -halfImagePartBoundaries[0][1]], but in 2D case halfImagePartBoundaries might be ==0, causes problem and you get a null slice.
    booleanNpArray_voxelsToCentraliseImPartsWithinBoundaries[halfImagePartBoundaries[0][0]: dims_of_scan[0] - halfImagePartBoundaries[0][1],
                                                            halfImagePartBoundaries[1][0]: dims_of_scan[1] - halfImagePartBoundaries[1][1],
                                                            halfImagePartBoundaries[2][0]: dims_of_scan[2] - halfImagePartBoundaries[2][1]] = 1
                                                            
    constrainedWithImageBoundariesMaskToSample = weightMapToSampleFrom * booleanNpArray_voxelsToCentraliseImPartsWithinBoundaries
    #normalize the probabilities to sum to 1, cause the function needs it as so.
    constrainedWithImageBoundariesMaskToSample = constrainedWithImageBoundariesMaskToSample / (1.0* np.sum(constrainedWithImageBoundariesMaskToSample))
    
    flattenedConstrainedWithImageBoundariesMaskToSample = constrainedWithImageBoundariesMaskToSample.flatten()
    
    #This is going to be a 3xNumberOfImagePartSamples array.
    indicesInTheFlattenArrayThatWereSampledAsCentralVoxelsOfImageParts = np.random.choice(  constrainedWithImageBoundariesMaskToSample.size,
                                                                                            size = numOfSegmentsToExtractForThisSubject,
                                                                                            replace=True,
                                                                                            p=flattenedConstrainedWithImageBoundariesMaskToSample)
    # np.unravel_index([listOfIndicesInFlattened], dims) returns a tuple of arrays (eg 3 of them if 3 dimImage), 
    # where each of the array in the tuple has the same shape as the listOfIndices. 
    # They have the r/c/z coords that correspond to the index of the flattened version.
    # So, coordsOfCentralVoxelsOfPartsSampled will be array of shape: 3(rcz) x numOfSegmentsToExtractForThisSubject.
    coordsOfCentralVoxelsOfPartsSampled = np.asarray(np.unravel_index(indicesInTheFlattenArrayThatWereSampledAsCentralVoxelsOfImageParts,
                                                                    constrainedWithImageBoundariesMaskToSample.shape #the shape of the roi_mask/scan.
                                                                    )
                                                    )
    # Array with shape: 3(rcz) x NumberOfImagePartSamples x 2.
    # Last dimension has [0] for lowest boundary of slice, and [1] for highest boundary. INCLUSIVE BOTH SIDES.
    sliceCoordsOfImagePartsSampled = np.zeros(list(coordsOfCentralVoxelsOfPartsSampled.shape) + [2], dtype="int32")
    sliceCoordsOfImagePartsSampled[:,:,0] = coordsOfCentralVoxelsOfPartsSampled - halfImagePartBoundaries[ :, np.newaxis, 0 ] #np.newaxis broadcasts. To broadcast the -+.
    sliceCoordsOfImagePartsSampled[:,:,1] = coordsOfCentralVoxelsOfPartsSampled + halfImagePartBoundaries[ :, np.newaxis, 1 ]
    
    # coordsOfCentralVoxelsOfPartsSampled: Array of dimensions 3(rcz) x NumberOfImagePartSamples.
    # sliceCoordsOfImagePartsSampled: Array of dimensions 3(rcz) x NumberOfImagePartSamples x 2. ...
    # ... The last dim has [0] for the lower boundary of the slice, and [1] for the higher boundary.
    # ... The slice coordinates returned are INCLUSIVE BOTH sides.
    imagePartsSampled = [coordsOfCentralVoxelsOfPartsSampled, sliceCoordsOfImagePartsSampled]
    return imagePartsSampled



def getImagePartFromSubsampledImageForTraining( dimsOfPrimarySegment,
                                                recFieldCnn,
                                                subsampledImageChannels,
                                                image_part_slices_coords,
                                                subSamplingFactor,
                                                subsampledImagePartDimensions
                                                ) :
    """
    This returns an image part from the sampled data, given the image_part_slices_coords,
    which has the coordinates where the normal-scale image part starts and ends (inclusive).
    (Actually, in this case, the right (end) part of image_part_slices_coords is not used.)
    
    The way it works is NOT optimal. From the beginning of the normal-resolution part,
    it goes further to the left 1 receptive-field and then forward xSubsamplingFactor receptive-fields.
    This stops it from being used with arbitrary size of subsampled segment (decoupled by the high-res segment).
    Now, the subsampled patch has to be of the same size as the normal-scale.
    To change this, I should find where THE FIRST TOP LEFT CENTRAL (predicted) VOXEL is, 
    and do the back-one-(sub)patch + front-3-(sub)patches from there, not from the begining of the patch.
    
    Current way it works (correct):
    If I have eg subsample factor=3 and 9 central-pred-voxels, I get 3 "central" voxels/patches for the
    subsampled-part. Straightforward. If I have a number of central voxels that is not an exact multiple of
    the subfactor, eg 10 central-voxels, I get 3+1 central voxels in the subsampled-part. 
    When the cnn is convolving them, they will get repeated to 4(last-layer-neurons)*3(factor) = 12, 
    and will get sliced down to 10, in order to have same dimension with the 1st pathway.
    """
    subsampledImageDimensions = subsampledImageChannels[0].shape
    
    subsampledChannelsForThisImagePart = np.ones((len(subsampledImageChannels),
                                                  subsampledImagePartDimensions[0],
                                                  subsampledImagePartDimensions[1],
                                                  subsampledImagePartDimensions[2]),
                                                 dtype = 'float32')
    
    numberOfCentralVoxelsClassifiedForEachImagePart_rDim = dimsOfPrimarySegment[0] - recFieldCnn[0] + 1
    numberOfCentralVoxelsClassifiedForEachImagePart_cDim = dimsOfPrimarySegment[1] - recFieldCnn[1] + 1
    numberOfCentralVoxelsClassifiedForEachImagePart_zDim = dimsOfPrimarySegment[2] - recFieldCnn[2] + 1
    
    #Calculate the slice that I should get, and where I should put it in the imagePart (eg if near the borders, and I cant grab a whole slice-imagePart).
    rSlotsPreviously = ((subSamplingFactor[0]-1)//2)*recFieldCnn[0] if subSamplingFactor[0]%2==1 \
                                                else (subSamplingFactor[0]-2)//2*recFieldCnn[0] + recFieldCnn[0]//2
    cSlotsPreviously = ((subSamplingFactor[1]-1)//2)*recFieldCnn[1] if subSamplingFactor[1]%2==1 \
                                                else (subSamplingFactor[1]-2)//2*recFieldCnn[1] + recFieldCnn[1]//2
    zSlotsPreviously = ((subSamplingFactor[2]-1)//2)*recFieldCnn[2] if subSamplingFactor[2]%2==1 \
                                                else (subSamplingFactor[2]-2)//2*recFieldCnn[2] + recFieldCnn[2]//2
    #1*17
    rToCentralVoxelOfAnAveragedArea = subSamplingFactor[0]//2 if subSamplingFactor[0]%2==1 else (subSamplingFactor[0]//2 - 1) #one closer to the beginning of dim. Same happens when I get parts of image.
    cToCentralVoxelOfAnAveragedArea = subSamplingFactor[1]//2 if subSamplingFactor[1]%2==1 else (subSamplingFactor[1]//2 - 1)
    zToCentralVoxelOfAnAveragedArea =  subSamplingFactor[2]//2 if subSamplingFactor[2]%2==1 else (subSamplingFactor[2]//2 - 1)
    #This is where to start taking voxels from the subsampled image. From the beginning of the imagePart(1 st patch)...
    #... go forward a few steps to the voxel that is like the "central" in this subsampled (eg 3x3) area. 
    #...Then go backwards -Patchsize to find the first voxel of the subsampled. 
    rlow = image_part_slices_coords[0][0] + rToCentralVoxelOfAnAveragedArea - rSlotsPreviously#These indices can run out of image boundaries. I ll correct them afterwards.
    #If the patch is 17x17, I want a 17x17 subsampled Patch. BUT if the imgPART is 25x25 (9voxClass), I want 3 subsampledPatches in my subsampPart to cover this area!
    #That is what the last term below is taking care of.
    #CAST TO INT because ceil returns a float, and later on when computing rHighNonInclToPutTheNotPaddedInSubsampledImPart I need to do INTEGER DIVISION.
    rhighNonIncl = int(rlow + subSamplingFactor[0]*recFieldCnn[0] + (math.ceil((numberOfCentralVoxelsClassifiedForEachImagePart_rDim*1.0)/subSamplingFactor[0]) - 1) * subSamplingFactor[0]) # excluding index in segment
    clow = image_part_slices_coords[1][0] + cToCentralVoxelOfAnAveragedArea - cSlotsPreviously
    chighNonIncl = int(clow + subSamplingFactor[1]*recFieldCnn[1] + (math.ceil((numberOfCentralVoxelsClassifiedForEachImagePart_cDim*1.0)/subSamplingFactor[1]) - 1) * subSamplingFactor[1])
    zlow = image_part_slices_coords[2][0] + zToCentralVoxelOfAnAveragedArea - zSlotsPreviously
    zhighNonIncl = int(zlow + subSamplingFactor[2]*recFieldCnn[2] + (math.ceil((numberOfCentralVoxelsClassifiedForEachImagePart_zDim*1.0)/subSamplingFactor[2]) - 1) * subSamplingFactor[2])
        
    rlowCorrected = max(rlow, 0)
    clowCorrected = max(clow, 0)
    zlowCorrected = max(zlow, 0)
    rhighNonInclCorrected = min(rhighNonIncl, subsampledImageDimensions[0])
    chighNonInclCorrected = min(chighNonIncl, subsampledImageDimensions[1])
    zhighNonInclCorrected = min(zhighNonIncl, subsampledImageDimensions[2]) #This gave 7
    
    rLowToPutTheNotPaddedInSubsampledImPart = 0 if rlow >= 0 else abs(rlow)//subSamplingFactor[0]
    cLowToPutTheNotPaddedInSubsampledImPart = 0 if clow >= 0 else abs(clow)//subSamplingFactor[1]
    zLowToPutTheNotPaddedInSubsampledImPart = 0 if zlow >= 0 else abs(zlow)//subSamplingFactor[2]
    
    dimensionsOfTheSliceOfSubsampledImageNotPadded = [  int(math.ceil((rhighNonInclCorrected - rlowCorrected)*1.0/subSamplingFactor[0])),
                                                        int(math.ceil((chighNonInclCorrected - clowCorrected)*1.0/subSamplingFactor[1])),
                                                        int(math.ceil((zhighNonInclCorrected - zlowCorrected)*1.0/subSamplingFactor[2]))
                                                        ]
    
    #I now have exactly where to get the slice from and where to put it in the new array.
    for channel_i in range(len(subsampledImageChannels)) :
        intensityZeroOfChannel = calculateTheZeroIntensityOf3dImage(subsampledImageChannels[channel_i])        
        subsampledChannelsForThisImagePart[channel_i] *= intensityZeroOfChannel
        
        sliceOfSubsampledImageNotPadded = subsampledImageChannels[channel_i][   rlowCorrected : rhighNonInclCorrected : subSamplingFactor[0],
                                                                                clowCorrected : chighNonInclCorrected : subSamplingFactor[1],
                                                                                zlowCorrected : zhighNonInclCorrected : subSamplingFactor[2]
                                                                            ]
        subsampledChannelsForThisImagePart[
            channel_i,
            rLowToPutTheNotPaddedInSubsampledImPart : rLowToPutTheNotPaddedInSubsampledImPart+dimensionsOfTheSliceOfSubsampledImageNotPadded[0],
            cLowToPutTheNotPaddedInSubsampledImPart : cLowToPutTheNotPaddedInSubsampledImPart+dimensionsOfTheSliceOfSubsampledImageNotPadded[1],
            zLowToPutTheNotPaddedInSubsampledImPart : zLowToPutTheNotPaddedInSubsampledImPart+dimensionsOfTheSliceOfSubsampledImageNotPadded[2]] = sliceOfSubsampledImageNotPadded
            
    #placeholderReturn = np.ones([3,19,19,19], dtype="float32") #channel, dims 
    return subsampledChannelsForThisImagePart



def shuffleSegments(  channs_of_samples_per_path, lbls_predicted_part_of_samples ) :
    
    n_paths_taking_inp = len(channs_of_samples_per_path)
    inp_to_zip = [ sublist_for_path for sublist_for_path in channs_of_samples_per_path ]
    inp_to_zip += [ lbls_predicted_part_of_samples ]
    
    combined = list(zip(*inp_to_zip)) #list() for python3 compatibility, as range cannot get assignment in shuffle()
    random.shuffle(combined)
    sublists_with_shuffled_samples = list(zip(*combined))
    
    shuffled_channs_of_samples_per_path = [ sublist_for_path for sublist_for_path in sublists_with_shuffled_samples[:n_paths_taking_inp] ]
    shuffled_lbls_predicted_part_of_samples = sublists_with_shuffled_samples[n_paths_taking_inp]
    
    return (shuffled_channs_of_samples_per_path, shuffled_lbls_predicted_part_of_samples)


# I must merge this with function: extractSegmentsGivenSliceCoords() that is used for Testing! Should be easy!
# This is used in training/val only.
def extractSegmentGivenSliceCoords(train_or_val,
                                   cnn3d,
                                   coord_center,
                                   channels,
                                   gt_lbl_img) :
    # channels: numpy array [ n_channels, x, y, z ]
    # coord_center: indeces of the central voxel for the patch to be extracted.
    
    channs_of_sample_per_path = []
    # Sampling
    for pathway in cnn3d.pathways[:1] : #Hack. The rest of this loop can work for the whole .pathways...
        # ... BUT the loop does not check what happens if boundaries are out of limits, to fill with zeros. This is done in getImagePartFromSubsampledImageForTraining().
        #... Update it in a nice way to be done here, and then take getImagePartFromSubsampledImageForTraining() out and make loop go for every pathway.
        
        if pathway.pType() == pt.FC :
            continue
        subSamplingFactor = pathway.subsFactor()
        pathwayInputShapeRcz = pathway.getShapeOfInput(train_or_val)[2:]
        leftBoundaryRcz = [ coord_center[0] - subSamplingFactor[0]*(pathwayInputShapeRcz[0]-1)//2,
                            coord_center[1] - subSamplingFactor[1]*(pathwayInputShapeRcz[1]-1)//2,
                            coord_center[2] - subSamplingFactor[2]*(pathwayInputShapeRcz[2]-1)//2]
        rightBoundaryRcz = [leftBoundaryRcz[0] + subSamplingFactor[0]*pathwayInputShapeRcz[0],
                            leftBoundaryRcz[1] + subSamplingFactor[1]*pathwayInputShapeRcz[1],
                            leftBoundaryRcz[2] + subSamplingFactor[2]*pathwayInputShapeRcz[2]]
        
        channelsForThisImagePart = channels[:,
                                            leftBoundaryRcz[0] : rightBoundaryRcz[0] : subSamplingFactor[0],
                                            leftBoundaryRcz[1] : rightBoundaryRcz[1] : subSamplingFactor[1],
                                            leftBoundaryRcz[2] : rightBoundaryRcz[2] : subSamplingFactor[2]]
        
        channs_of_sample_per_path.append(channelsForThisImagePart)
        
    # Extract the samples for secondary pathways. This whole for can go away, if I update above code to check to slices out of limits.
    for pathway_i in range(len(cnn3d.pathways)) : # Except Normal 1st, cause that was done already.
        if cnn3d.pathways[pathway_i].pType() == pt.FC or cnn3d.pathways[pathway_i].pType() == pt.NORM:
            continue
        #this datastructure is similar to channelsForThisImagePart, but contains voxels from the subsampled image.
        dimsOfPrimarySegment = cnn3d.pathways[pathway_i].getShapeOfInput(train_or_val)[2:]
        slicesCoordsOfSegmForPrimaryPathway = [ [leftBoundaryRcz[0], rightBoundaryRcz[0]-1], [leftBoundaryRcz[1], rightBoundaryRcz[1]-1], [leftBoundaryRcz[2], rightBoundaryRcz[2]-1] ] # rightmost  are placeholders here.
        channsForThisSubsampledPartAndPathway = getImagePartFromSubsampledImageForTraining(dimsOfPrimarySegment=dimsOfPrimarySegment,
                                                                                        recFieldCnn=cnn3d.recFieldCnn,
                                                                                        subsampledImageChannels=channels,
                                                                                        image_part_slices_coords=slicesCoordsOfSegmForPrimaryPathway,
                                                                                        subSamplingFactor=cnn3d.pathways[pathway_i].subsFactor(),
                                                                                        subsampledImagePartDimensions=cnn3d.pathways[pathway_i].getShapeOfInput(train_or_val)[2:]
                                                                                        )
        
        channs_of_sample_per_path.append(channsForThisSubsampledPartAndPathway)
        
    # Get ground truth labels for training.
    numOfCentralVoxelsClassifRcz = cnn3d.finalTargetLayer_outputShape[train_or_val][2:]
    leftBoundaryRcz = [ coord_center[0] - (numOfCentralVoxelsClassifRcz[0]-1)//2,
                        coord_center[1] - (numOfCentralVoxelsClassifRcz[1]-1)//2,
                        coord_center[2] - (numOfCentralVoxelsClassifRcz[2]-1)//2]
    rightBoundaryRcz = [leftBoundaryRcz[0] + numOfCentralVoxelsClassifRcz[0],
                        leftBoundaryRcz[1] + numOfCentralVoxelsClassifRcz[1],
                        leftBoundaryRcz[2] + numOfCentralVoxelsClassifRcz[2]]
    lbls_predicted_part_of_sample = gt_lbl_img[ leftBoundaryRcz[0] : rightBoundaryRcz[0],
                                                leftBoundaryRcz[1] : rightBoundaryRcz[1],
                                                leftBoundaryRcz[2] : rightBoundaryRcz[2] ]
    
    # Make COPIES of the segments, instead of having a VIEW (slice) of them. This is so that the the whole volume are afterwards released from RAM.
    channs_of_sample_per_path = [ np.array(pathw_channs, copy=True, dtype='float32') for pathw_channs in channs_of_sample_per_path  ]
    lbls_predicted_part_of_sample = np.copy(lbls_predicted_part_of_sample)
    
    return (channs_of_sample_per_path, lbls_predicted_part_of_sample)




#################################################################################################################################
#                                                                                                                               #
#       Below are functions for testing only. There is duplication with training. They are not the same, but could be merged.   #
#                                                                                                                               #
#################################################################################################################################

# This is very similar to sample_coords_of_segments() I believe, which is used for training. Consider way to merge them.
def getCoordsOfAllSegmentsOfAnImage(log,
                                    dimsOfPrimarySegment, # RCZ dims of input to primary pathway (NORMAL). Which should be the first one in .pathways.
                                    strideOfSegmentsPerDimInVoxels,
                                    batch_size,
                                    channelsOfImageNpArray,
                                    roi_mask
                                    ) :
    # channelsOfImageNpArray: np array [n_channels, x, y, z]
    log.print3("Starting to (tile) extract Segments from the images of the subject for Segmentation...")
    
    sliceCoordsOfSegmentsToReturn = []
    
    niiDimensions = list(channelsOfImageNpArray[0].shape) # Dims of the volumes
    
    zLowBoundaryNext=0; zAxisCentralPartPredicted = False;
    while not zAxisCentralPartPredicted :
        zFarBoundary = min(zLowBoundaryNext+dimsOfPrimarySegment[2], niiDimensions[2]) #Excluding.
        zLowBoundary = zFarBoundary - dimsOfPrimarySegment[2]
        zLowBoundaryNext = zLowBoundaryNext + strideOfSegmentsPerDimInVoxels[2]
        zAxisCentralPartPredicted = False if zFarBoundary < niiDimensions[2] else True #THIS IS THE IMPORTANT CRITERION.
        
        cLowBoundaryNext=0; cAxisCentralPartPredicted = False;
        while not cAxisCentralPartPredicted :
            cFarBoundary = min(cLowBoundaryNext+dimsOfPrimarySegment[1], niiDimensions[1]) #Excluding.
            cLowBoundary = cFarBoundary - dimsOfPrimarySegment[1]
            cLowBoundaryNext = cLowBoundaryNext + strideOfSegmentsPerDimInVoxels[1]
            cAxisCentralPartPredicted = False if cFarBoundary < niiDimensions[1] else True
            
            rLowBoundaryNext=0; rAxisCentralPartPredicted = False;
            while not rAxisCentralPartPredicted :
                rFarBoundary = min(rLowBoundaryNext+dimsOfPrimarySegment[0], niiDimensions[0]) #Excluding.
                rLowBoundary = rFarBoundary - dimsOfPrimarySegment[0]
                rLowBoundaryNext = rLowBoundaryNext + strideOfSegmentsPerDimInVoxels[0]
                rAxisCentralPartPredicted = False if rFarBoundary < niiDimensions[0] else True
                
                if isinstance(roi_mask, (np.ndarray)) : #In case I pass a brain-mask, I ll use it to only predict inside it. Otherwise, whole image.
                    if not np.any(roi_mask[rLowBoundary:rFarBoundary,
                                            cLowBoundary:cFarBoundary,
                                            zLowBoundary:zFarBoundary]
                                  ) : #all of it is out of the brain so skip it.
                        continue
                    
                sliceCoordsOfSegmentsToReturn.append([ [rLowBoundary, rFarBoundary-1], [cLowBoundary, cFarBoundary-1], [zLowBoundary, zFarBoundary-1] ])
                
    #I need to have a total number of image-parts that can be exactly-divided by the 'batch_size'. For this reason, I add in the far end of the list multiple copies of the last element.
    total_number_of_image_parts = len(sliceCoordsOfSegmentsToReturn)
    number_of_imageParts_missing_for_exact_division =  batch_size - total_number_of_image_parts%batch_size if total_number_of_image_parts%batch_size != 0 else 0
    for extra_useless_image_part_i in range(number_of_imageParts_missing_for_exact_division) :
        sliceCoordsOfSegmentsToReturn.append(sliceCoordsOfSegmentsToReturn[-1])
        
    #I think that since the parts are acquired in a certain order and are sorted this way in the list, it is easy
    #to know which part of the image they came from, as it depends only on the stride-size and the imagePart size.
    
    log.print3("Finished (tiling) extracting Segments from the images of the subject for Segmentation.")
    
    # sliceCoordsOfSegmentsToReturn: list with 3 dimensions. numberOfSegments x 3(rcz) x 2 (lower and upper limit of the segment, INCLUSIVE both sides)
    return sliceCoordsOfSegmentsToReturn



# I must merge this with function: extractSegmentGivenSliceCoords() that is used for Training/Validation! Should be easy!
# This is used in testing only.
def extractSegmentsGivenSliceCoords(cnn3d,
                                    sliceCoordsOfSegmentsToExtract,
                                    channelsOfImageNpArray,
                                    recFieldCnn) :
    # channelsOfImageNpArray: numpy array [ n_channels, x, y, z ]
    numberOfSegmentsToExtract = len(sliceCoordsOfSegmentsToExtract)
    channsForSegmentsPerPathToReturn = [ [] for i in range(cnn3d.getNumPathwaysThatRequireInput()) ] # [pathway, image parts, channels, r, c, z]
    dimsOfPrimarySegment = cnn3d.pathways[0].getShapeOfInput("test")[2:] # RCZ dims of input to primary pathway (NORMAL). Which should be the first one in .pathways.
    
    for segment_i in range(numberOfSegmentsToExtract) :
        rLowBoundary = sliceCoordsOfSegmentsToExtract[segment_i][0][0]; rFarBoundary = sliceCoordsOfSegmentsToExtract[segment_i][0][1]
        cLowBoundary = sliceCoordsOfSegmentsToExtract[segment_i][1][0]; cFarBoundary = sliceCoordsOfSegmentsToExtract[segment_i][1][1]
        zLowBoundary = sliceCoordsOfSegmentsToExtract[segment_i][2][0]; zFarBoundary = sliceCoordsOfSegmentsToExtract[segment_i][2][1]
        # segment for primary pathway
        channsForPrimaryPathForThisSegm = channelsOfImageNpArray[:,
                                                                rLowBoundary:rFarBoundary+1,
                                                                cLowBoundary:cFarBoundary+1,
                                                                zLowBoundary:zFarBoundary+1
                                                                ]
        channsForSegmentsPerPathToReturn[0].append(channsForPrimaryPathForThisSegm)
        
        #Subsampled pathways
        for pathway_i in range(len(cnn3d.pathways)) : # Except Normal 1st, cause that was done already.
            if cnn3d.pathways[pathway_i].pType() == pt.FC or cnn3d.pathways[pathway_i].pType() == pt.NORM:
                continue
            slicesCoordsOfSegmForPrimaryPathway = [ [rLowBoundary, rFarBoundary-1], [cLowBoundary, cFarBoundary-1], [zLowBoundary, zFarBoundary-1] ] #the right hand values are placeholders in this case.
            channsForThisSubsPathForThisSegm = getImagePartFromSubsampledImageForTraining(  dimsOfPrimarySegment=dimsOfPrimarySegment,
                                                                                            recFieldCnn=recFieldCnn,
                                                                                            subsampledImageChannels=channelsOfImageNpArray,
                                                                                            image_part_slices_coords=slicesCoordsOfSegmForPrimaryPathway,
                                                                                            subSamplingFactor=cnn3d.pathways[pathway_i].subsFactor(),
                                                                                            subsampledImagePartDimensions=cnn3d.pathways[pathway_i].getShapeOfInput("test")[2:]
                                                                                            )
            channsForSegmentsPerPathToReturn[pathway_i].append(channsForThisSubsPathForThisSegm)
            
    return channsForSegmentsPerPathToReturn



###########################################
# Checks whether the data is as expected  #
###########################################

def check_gt_vs_num_classes(log, img_gt, num_classes):
    id_str = "["+str(os.getpid())+"]"
    max_in_gt = np.max(img_gt)
    if np.max(img_gt) > num_classes-1 : # num_classes includes background=0
        msg =  id_str+" ERROR:\t GT labels included a label value ["+str(max_in_gt)+"] greater than what CNN expects."+\
                "\n\t In model-config the number of classes was specified as ["+str(num_classes)+"]."+\
                "\n\t Check your data or change the number of classes in model-config."+\
                "\n\t Note: number of classes in model config should include the background as a class."
        log.print3(msg)
        raise ValueError(msg)




