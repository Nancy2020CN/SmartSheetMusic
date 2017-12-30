import librosa as lb
import numpy as np
import os
import utils_audio_transcript as utils
import pretty_midi
import matplotlib.pyplot as plt
from math import ceil

class CellList():    
    '''
    Class to handle a triplet (s, w, sw) of cells.
    Cells are represented as tuples of integers.
    '''
    def __init__(self, s=None, w=None, sw=None):
        '''
        Fill the triplet.
        The idx is not None only if the cell actually exists and is non-nan. 
        '''        
        self.idxs = [s, w, sw]
        
    def get(self, idx, cum_distance):
        '''
        Get the cum_distance based on a target step (where we come from).
        idx = 0 --> s
        idx = 1 --> w
        idx = 2 --> sw
        '''
        if self.idxs[idx] is None:
            return(np.nan)        
        else:
            return(cum_distance[self.idxs[idx]])
    
    def find_best_step(self, cum_distance, cur_cost, diag_cost,alpha=1.0):
        '''
        The function computes the local DTW step, adjusting for the diagonal penalty.
        We can also have either cumulative distance (alpha = 1) or EWMA distance (alpha < 1).        
        It returns the optimal distance and the optimal step.
        '''                
            
        d = np.array([cur_cost + alpha*self.get(0, cum_distance), 
                      cur_cost + alpha*self.get(1, cum_distance), 
                      diag_cost*cur_cost + alpha*self.get(2, cum_distance)])
                            
        argmin = np.nanargmin(d)            
                    
        return(d[argmin], argmin)
    
    @staticmethod
    def step_to_idxs(step, idx_act, idx_est):
        '''
        Map a step in (0,1,2) to the change in (idx_act, idx_est)
        ''' 
        if step == 0:
            return(idx_act-1, idx_est)
        elif step == 1:
            return(idx_act, idx_est-1)
        elif step == 2:
            return(idx_act-1, idx_est-1)
        else:
            raise(ValueError('Step not valid'))
        
class OnlineDTW():
    '''
    Implementation of the online DTW matcher as per S. Dixon.
    Resources:
    http://eecs.qmul.ac.uk/~simond/pub/2005/dafx05.pdf
    https://www.eecs.qmul.ac.uk/~simond/pub/2010/Macrae-Dixon-ISMIR-2010-WindowedTimeWarping.pdf
    http://www.cp.jku.at/research/papers/Arzt_Masterarbeit_2007.pdf
    https://code.soundsoftware.ac.uk/projects/match/repository/entry/at/ofai/music/match/Finder.java
    '''
    def __init__(self, 
                 features_act,
                 distance_fcn=utils.distance_chroma, 
                 diag_cost=1.2, 
                 alpha=1.0, 
                 smoothing_parameter=1.0,
                 positions_type='min',
                 light_run=True):
        
        
        self.features_act = features_act 
        self.nb_obs_feature_act = features_act.shape[0]
        self.nb_bin_feature = features_act.shape[1]
        
        # Current position in the act / est data.
        # Set to -1 as no data has been processed.
        self.idx_act = -1
        self.idx_est = -1
        
        # Initialise position and position_sec and position_tick.  
        # They represent the expected position in the Midi file.
        # The best distance is the total alignment distance (cum or EWMA)
        # We have:  best_distance = [x[-1] for x in best_paths_distance] 
        self.position = 0
        self.positions = []
        
        # Distance function
        self.distance_fcn = distance_fcn

        # DTW parameters
        self.search_width = 100 # width of the band to search the optimal alignment (in frames)
        self.min_data = 20 # min number of input frames to compute before doing anything        
        self.diag_cost = diag_cost # the cost for the diagonal (1.0<=x<=2.0, may be set to less than 2 to let the algorithm favour diagonal moves)
        self.alpha = alpha # Alpha enable to either use cumulative distance (alpha = 1) or EWMA distance (alpha < 1).
        self.smoothing_parameter = smoothing_parameter
        
        # Position reported
        self.positions_type = positions_type           
                
        # Boolean to check if the new input has been processed
        self.input_advanced = False
        
        # Initialise large empty matrices for features_est and for the cumulative distance matrix
        self.features_est = np.zeros((max(self.nb_obs_feature_act*2, 2000), self.nb_bin_feature))
        self.features_est.fill(np.nan)
        self.cum_distance = np.zeros((self.nb_obs_feature_act, self.features_est.shape[0]), dtype='float16')
        self.cum_distance.fill(np.nan)
        
        # Initialise large empty matrices for the steps
        self.steps = np.zeros((self.nb_obs_feature_act, self.features_est.shape[0]), dtype='int16')
        self.steps.fill(np.iinfo(np.int16).min)
        
        # Initialise the best_paths, in order to keep the best path at each iteration
        # (one iteration correspond to one update of the live feed)
        self.light_run = light_run # Flag to backtrack or not 
        self.best_paths = []
        self.best_paths_distance = []
        
    def select_advance_direction(self):        
        
        idx_est = self.idx_est
        idx_act = self.idx_act
            
        if idx_est < self.min_data or idx_act < self.min_data:
            return(0)  
        
        if idx_act >= self.nb_obs_feature_act - 1:
            return(2)        
        
        # Check if the minimum distance is in the last row or in the last column
        arg_min_row = np.nanargmin(self.cum_distance[idx_act,0:idx_est+1])
        arg_min_col = np.nanargmin(self.cum_distance[0:idx_act+1,idx_est])
                
        if arg_min_row == idx_est and arg_min_col == idx_act:
            direction = 0 # compute both row and column
        elif self.cum_distance[idx_act,arg_min_row] < self.cum_distance[arg_min_col,idx_est]:
            direction = 1 # compute next row
        else:
            direction = 2 # compute next column  
                        
        return(direction)

    def find_cells(self, i, j):
        '''
        The function identifies the cells that should be evaluated.
        Refer to Figure 2 of http://eecs.qmul.ac.uk/~simond/pub/2005/dafx05.pdf
        for detail on the possible cases.                  
        '''
         
        if i-1 < 0:
            return(CellList(w=(i, j-1)))            
             
        if j-1 < 0:
            return(CellList(s=(i-1,j)))
         
        isnan_s = np.isnan(self.cum_distance[i-1, j])
        isnan_w = np.isnan(self.cum_distance[i, j-1])
        isnan_sw = np.isnan(self.cum_distance[i-1, j-1])       
         
        # Standard case: compute everything        
        if not isnan_s and not isnan_w and not isnan_sw:
            return(CellList(s=(i-1, j), w=(i, j-1), sw=(i-1,j-1)))
         
        # Edge case: nothing to compute.  
        elif isnan_s and isnan_w and isnan_sw:
            ValueError('Could not find any valid cell leading to  {}, {}'.format(i, j))            
         
        # Otherwise, find the relevant cells to compute
        elif isnan_s and isnan_sw:
            return(CellList(w=(i,j-1)))
         
        elif isnan_w and isnan_sw:
            return(CellList(s=(i-1,j)))
         
        elif isnan_s:
            return(CellList(w=(i,j-1), sw=(i-1,j-1)))
         
        elif isnan_w:
            return(CellList(s=(i-1,j), sw=(i-1,j-1)))
                            
    def update_cum_distance(self, direction):
        '''
        Taking the existing cum_distance matrix and the search direction, this function
        updates the cum_distance by finding the (local) min distance path.
        '''
        
        # For the first iteration, just take the distance between the two chromagrams
        if self.idx_act == -1 and self.idx_est == -1:
            self.idx_act += 1
            self.idx_est += 1
            self.cum_distance[self.idx_act, self.idx_est] = self.distance_fcn(self.features_act[self.idx_act, :], 
                                                                                  self.features_est[self.idx_est, :])
            self.input_advanced = True            
            return                        
        
        if direction == 0:
            # Update in both directions, but start by updating in the live feed direction
            # (not required, but this is what Dixon does)
            self.update_cum_distance(2)
            self.update_cum_distance(1)
            return
                        
        if direction == 1:
            # Advance the score
            self.idx_act += 1
            idx_act = self.idx_act
            idx_est = self.idx_est
            
            for k in np.arange(max(idx_est-self.search_width+1, 0), idx_est+1):                
                curr_cost = self.distance_fcn(self.features_act[idx_act, :], self.features_est[k, :])                
                cells = self.find_cells(idx_act, k)                                                                 
                (self.cum_distance[idx_act, k], self.steps[idx_act, k]) = cells.find_best_step(self.cum_distance, 
                                                                                               curr_cost, 
                                                                                               self.diag_cost,
                                                                                               alpha=self.alpha)
                
            return
            
        if direction == 2:
            # Advance the live feed
            self.idx_est += 1
            self.input_advanced = True  
            idx_act = self.idx_act
            idx_est = self.idx_est

            min_idx_act = max(idx_act-int(ceil(self.search_width/2.0))+1, 0)
            max_idx_act = min(idx_act+int(ceil(self.search_width/2.0))+1, self.nb_obs_feature_act)
            for k in np.arange(min_idx_act, max_idx_act):                
                curr_cost = self.distance_fcn(self.features_act[k, :], self.features_est[idx_est, :])
                cells = self.find_cells(k, idx_est)
                (self.cum_distance[k, idx_est], self.steps[k, idx_est]) = cells.find_best_step(self.cum_distance, 
                                                                                               curr_cost, 
                                                                                               self.diag_cost,
                                                                                               alpha=self.alpha)                
            return

    def update_best_path(self, max_backtrack=np.inf):
        '''
        Based on the cum distance matrix, this function finds the best path
        Running this function is not required for the main loop, it serves mainly 
        for ex-post analysis.
        ''' 
 
        idx_est = self.idx_est               
        idx_act = self.position
 
        best_path = ([idx_act], [idx_est])
        best_path_distance = [self.cum_distance[idx_act, idx_est]]       
             
        # Iterate until the starting point
        cnt_backtract = 0
        while cnt_backtract < max_backtrack and idx_est > 0 :                                    
             
            (idx_act, idx_est_new) = CellList.step_to_idxs(self.steps[idx_act, idx_est], idx_act, idx_est)                         
            cnt_backtract += idx_est - idx_est_new
             
            if idx_est - idx_est_new != 0: 
                best_path_distance.append(self.cum_distance[idx_act,idx_est_new])
                best_path[0].append(idx_act)
                best_path[1].append(idx_est_new)
                 
            idx_est = idx_est_new
             
        # Keep the best path for successive iterations
        # (the history of the best paths could be dropped at a later stage) 
        self.best_paths.append(best_path)
        self.best_paths_distance.append(list(reversed(best_path_distance)))            
        
    def update_position(self):        
        '''
        Append idx_act to the list of estimated positions (and the equivalent in seconds). 
        In the case we have the midi_object available, also report the position in ticks
        '''
        position_type = self.positions_type
        
        if position_type == 'idx_act':
            self.position = self.idx_act
        elif position_type == 'min': 
            self.position = np.nanargmin(self.cum_distance[:,self.idx_est])
        elif position_type == 'adj_min':            
            adj_distance = self.cum_distance[:,self.idx_est] / np.arange(1,self.cum_distance.shape[0]+1)
            self.position = np.nanargmin(adj_distance)
        
        self.positions.append(self.position)  
        
    def plot_dtw_distance(self, paths=[-1]):
        '''
        Plot the cumulative distance matrix as a heat map and the best path.
        
        path_nb is the number of the path we want to plot (-1 for the final path). 
        '''
        utils.plot_dtw_distance(self.cum_distance)

        for k in paths:
            plt.plot(self.best_paths[k].cols, self.best_paths[k].rows, color='black')                                              

    def main_dtw(self, features_est_new): 
        '''
        Called for every new frame of the live feed, for which we run the online DTW
        '''
        
        if features_est_new.shape != (self.nb_bin_feature,):
            raise(ValueError('Incompatible shape, expect 1D array with {} items'.format(self.nb_bin_feature)))
        
        # Store the new features
        self.features_est[self.idx_est+1, :] = features_est_new
                                    
        # Run the main loop until the input has been advanced
        self.input_advanced = False        
        while not self.input_advanced:                     
            direction = self.select_advance_direction()
            self.update_cum_distance(direction)        
                    
        # Update the estimated current position
        self.update_position()    
        
        # Find the best path, if need be    
        if not self.light_run:                
            self.update_best_path(10)            

class Matcher():
    '''
    Top-level class for the score following procedure. 
    This class serves as a wrapper around the online DTW.
    '''
    def __init__(self, wd, filename, sr, hop_length,                  
                 dtw_kwargs = {}, 
                 compute_chromagram_fcn=lb.feature.chroma_stft,                
                 compute_chromagram_fcn_kwargs={'tuning':0.0}, 
                 chromagram_mode=0,
                 chromagram_act=None):
                         
        
        # Start by fixing the sample rate and hop size for the spectrum
        # decomposition
        self.sr = sr
        self.hop_length = hop_length  
        
        # Initialise the placeholder for the estimated audio data (only the most recent)
        # This is required in the online mode, as we may want to compute the chromagram  
        # with more data that just the last chunk.
        self.audio_data = np.array([])         
        self.min_len_sample = utils.calc_nb_sample_stft(self.sr, self.hop_length, 3.0) # Nb of samples to compute the chromagram 
        
        # Set the function used to compute the chromagram and its parameters
        self.compute_chromagram_fcn = compute_chromagram_fcn
        self.compute_chromagram_fcn_kwargs = compute_chromagram_fcn_kwargs
        self.chromagram_mode = chromagram_mode
        
        # Check if we need to store the chromagram to disk and retrieve it when it exists
        self.store_chromagram = False  
        
        # Check whether we have the '.mid' or '.wav' as input.
        _, self.file_extension = os.path.splitext(filename)        
        if not (self.file_extension == '.mid' or self.file_extension == '.wav'):
            raise ValueError('Input file need to be either .mid or .wav')
        
        # Load the .wav file and turn into chromagram.
        if chromagram_act is None:
            chromagram_act = self.get_chromagram(wd, filename)  

        self.positions_sec = []
        self.position_tick = 0
        self.best_distance = []        
        
        # Set the random distance threshold, for the position filtering.
        # Make sure to update if we change the distance. 
        self.random_distance_threshold = utils.calc_mean_random_distance(chromagram_act)
        self.position_filtered = -1
        self.positions_filtered = []
        self.positions_sec_filtered = []
        self.position_tick_filtered = 0          
        
        # Finally, initialise the DTW engine
        self.dtw = OnlineDTW(chromagram_act, **dtw_kwargs)            
        
    def get_chromagram(self, wd, filename):
        '''
        The function can take as input a 'wav' or a '.mid' file.
        in the latter case, we have to generate the corresponding '.wav'.  
                
        Get the chromagram from the disk or process the '.wav' file and write 
        the chromagram to disk if need be.
        '''
        
        # Build a specific key for the chromagram in case we want to store.
        suffix_filename = "_chromagram_S{}_H{}_fcn{}_mode{}_.npy".format(self.sr, 
                                                                         self.hop_length,
                                                                         self.compute_chromagram_fcn.__name__, 
                                                                         self.chromagram_mode)                                                                                                                                                                                                             
        
        filename_chomagram = wd + utils.unmake_file_format(filename, self.file_extension) + suffix_filename
        

        # We check if the chromagram exists on disk, otherwise we generate it.         
        if os.path.isfile(filename_chomagram) and self.store_chromagram:
            chromagram_act = np.load(filename_chomagram)
        else:
            # If the input file is a '.mid', we have to generate the '.wav'
            # Also, store the midi object, it will be useful to get the position in ticks
            if self.file_extension == '.mid':
                self.midi_obj = pretty_midi.PrettyMIDI(wd + filename)#filename_mid = filename                
                filename = utils.change_file_format(filename, '.mid', '.wav')                
                utils.write_wav(wd + filename, 
                                self.midi_obj.fluidsynth(self.sr, start_new_process32=utils.fluidsynth_start_new_process32()), 
                                rate = self.sr)
            
            # Now we are sure to have a '.wav', we can retrieve it.
            audio_data_wav = lb.core.load(wd + utils.make_file_format(filename, '.wav'), sr = self.sr)
            
            # Turn into chromagram
            chromagram_act = self.compute_chromagram(audio_data_wav[0])
            
            # Store if need be.                                                                                                                                                                                         
            if self.store_chromagram:                                                 
                np.save(filename_chomagram, self.chromagram_act)            

        return(chromagram_act)
        
    def compute_chromagram(self, y):
        '''
        Wrapper function to compute the chromagram.
        mode = 0: Return the raw chromagram
        mode = 1: Return the d_chromagram, i.e. the positive (time) difference
        mode = 2: Return [chromagram, d_chromagram] 
        '''
        
        if not (self.chromagram_mode==0 or self.chromagram_mode==1 or self.chromagram_mode==2): 
            raise ValueError('Mode needs to be in {0, 1, 2}')

        chromagram = self.compute_chromagram_fcn(y=np.array(y),
                                                 sr=self.sr,
                                                 hop_length=self.hop_length,
                                                 tuning=0.0,
                                                 **self.compute_chromagram_fcn_kwargs).T
                                            
        if self.chromagram_mode == 0:
            return(chromagram)
        else:
            d_chromagram = np.diff(chromagram, axis=0)
            d_chromagram = np.maximum(d_chromagram, 0.0)
            if self.chromagram_mode == 1:
                return(d_chromagram)
            else:
                return(np.hstack((chromagram[1:,:], d_chromagram)))
            
    
    def update_audio_data(self, new_audio_data):
        '''
        Update the buffer of audio data. We use this buffer to compute the live chromagram.
        '''
        
        # Check that we pass in the right type of data
        if new_audio_data.dtype is not np.dtype(utils.AUDIO_FORMAT_MAP[utils.AUDIO_FORMAT_DEFAULT][0]):
            raise(TypeError('Input audio data need to be {}!'.format(utils.AUDIO_FORMAT_DEFAULT)))
        
        
        # Make sure that the chunk size is a multiple of hop size
        if (len(new_audio_data) % self.hop_length) != 0:
            raise ValueError('Chunk size need to be a multiple of hop size')
        
        # Append the new data
        self.audio_data = np.append(self.audio_data, new_audio_data)
        
        # Trim. There are two possible cases:
        # - we added a lot of data , i.e. len(new_audio_data) > self.min_len_sample: we want to keep all the new added data
        # - we added a an online chunk of data , i.e. len(new_audio_data) < self.min_len_sample: in this case, we only keep 
        # the most recent samples. 
        self.audio_data = self.audio_data[max(len(self.audio_data) - max(self.min_len_sample, len(new_audio_data)), 0):len(self.audio_data)]                        
        
    def filter_position(self):
        '''
        TODO :  ADD COMMENTS AND FIX
        '''
        
        nb_frames_ma_distance = 3
        nb_frames_speed = 10
        
        cum_distance = np.concatenate((np.array([0.0]), np.array(self.best_distance)))
        delta_distance = cum_distance[1:]-self.alpha*cum_distance[0:-1]
        mean_delta_distance = np.mean(delta_distance[max(self.idx_est-nb_frames_ma_distance+1, 0):self.idx_est+1])
                
        bad_alignment = mean_delta_distance > 0.8*self.random_distance_threshold        
                            
        if bad_alignment:
            position_posterior = self.position_filtered
        else:
            positions = np.concatenate((np.array([-1]), np.array(self.positions)))
            positions = positions[max(self.idx_est-nb_frames_speed+2, 0):self.idx_est+2] # +2 since we prepended an item
            speed = np.mean(np.diff(positions))
            position_prior = self.position_filtered + speed
            position_posterior = position_prior*self.smoothing_parameter + (1-self.smoothing_parameter)*self.position  
             
        self.position_filtered = position_posterior
        self.positions_filtered.append(position_posterior)
        self.positions_sec_filtered.append(position_posterior * self.hop_length / float(self.sr))
        
        # Only possible if the input file was a '.mid' in the first place
        if self.file_extension == '.mid':
            self.position_tick_filtered = self.midi_obj.time_to_tick(self.positions_sec_filtered[-1])
            
            
    def plot_chromagrams(self):
        """
        Plot both the estimated and the actual chromagram for comparison.
        """
        
        axarr = plt.subplots(2, 1)[1]
        
        # Plot chromagram_act
        utils.plot_chromagram(self.chromagram_act, sr=self.sr, hop_length=self.hop_length, ax=axarr[0], xticks_sec=True)
        axarr[0].set_title('Chromagram act')
        
        # Plot chromagram_est (up to idx_est). Will need to amend to sr_est and hop_length_est at some point...
        utils.plot_chromagram(self.chromagram_est[0:self.idx_est,:], sr=self.sr, hop_length=self.hop_length, ax=axarr[1], xticks_sec=True)
        axarr[1].set_title('Chromagram est')  
           
        
    def match_batch_online(self, audio_data_est):
        '''
        Align a target raw audio.
        The raw audio needs to be sampled as per the matcher's sample rate.
        Though the process is offline because we pass in the entire audio set, the 
        actual alignment does not use any forward-looking data.
         
        Return a mapping between the times of the target audio vs times of the "true" audio.  
        '''
        
        self.match_online(audio_data_est)
        
        # Get the alignment output
        # Not sure whether we should add/take out one frame... Also, we should use matcher_tmp.sr_est once possible
        times_cor_est = np.arange(len(self.positions_sec)) * self.hop_length / float(self.sr) 
        times_ori_est = np.array(self.positions_sec)
         
        return(times_cor_est, times_ori_est)
    
    def match_batch_offline(self, audio_data_est):
        '''
        Use the librosa DTW implementation to perform offline matching.
        '''
        
        # Compute the chromagram, use the engine of the matcher to ensure that both chromagrams have 
        # been computed with the same procedure.            
        chromagram_est = self.compute_chromagram(audio_data_est)
        
        # Set up the diagonal cost        
        weights_mul = np.array([1.0*self.dtw.diag_cost, 1.0, 1.0])   
        
        # Run the DTW alignment
        [cum_distance, best_path] = lb.core.dtw(self.dtw.features_act.T, chromagram_est.T, weights_mul=weights_mul)
        
        # Get the alignment output, reverse the order of the paths
        times_ori_est = np.flip(best_path[:,0], 0) * self.hop_length / float(self.sr)         
        times_cor_est = np.flip(best_path[:,1], 0) * self.hop_length / float(self.sr)
                            
        return(times_cor_est, times_ori_est, {'cum_distance':cum_distance, 'best_path':best_path}) 
                                        
                                                        
    def match_online(self, new_audio_data_est):
        '''
        Run the online matching procedure for a chunk of audio data.
        '''
        
        # Add the new data to the buffer
        self.update_audio_data(new_audio_data_est)
        
        # Compute the chromagram for the entire buffer. 
        # This is not computationally optimal, (since len(new_audio_data_est) << len(self.audio_data))
        # but it is safer. We may need to reduce the buffer size if this computation is too expensive.       
        chromagram_est = self.compute_chromagram(self.audio_data)                                                               
        
        # Only run the matching over the last (len(new_audio_data)/self.hop_length) segments of the chromagram        
        idx_frames = np.arange(chromagram_est.shape[0] - len(new_audio_data_est) / self.hop_length, chromagram_est.shape[0])
        
        # Run the online matching procedure, one frame at a time
        for n in idx_frames:
            self.dtw.main_dtw(chromagram_est[n,:])
            
            self.positions_sec.append(self.dtw.position * self.hop_length / float(self.sr))
            
            # Only possible if the input file was a '.mid' in the first place
            if self.file_extension == '.mid':
                self.position_tick = self.midi_obj.time_to_tick(self.positions_sec[-1])   
