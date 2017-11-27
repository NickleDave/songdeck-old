import sys
import os
import pickle
from glob import glob
from configparser import ConfigParser
from datetime import datetime

import tensorflow as tf
import numpy as np
from sklearn.externals import joblib
import scipy.io

import cnn_bilstm.utils

config_file = sys.argv[1]
if not config_file.endswith('.ini'):
    raise ValueError('{} is not a valid config file, must have .ini extension'
                     .format(config_file))
config = ConfigParser()
config.read(config_file)

results_dirname = config['OUTPUT']['output_dir']
if not os.path.isdir(results_dirname):
    raise FileNotFoundError('{} directory is not found.'
                            .format(results_dirname))

timenow = datetime.now().strftime('%y%m%d_%H%M%S')
summary_dirname = os.path.join(results_dirname,
                               'summary_' + timenow)
os.makedirs(summary_dirname)


batch_size = int(config['NETWORK']['batch_size'])
time_steps = int(config['NETWORK']['time_steps'])

TRAIN_SET_DURS = [int(element)
                  for element in
                  config['TRAIN']['train_set_durs'].split(',')]

num_replicates = int(config['TRAIN']['replicates'])
REPLICATES = range(num_replicates)
normalize_spectrograms = config.getboolean('DATA', 'normalize_spectrograms')

spect_params = {}
for spect_param_name in ['freq_cutoffs', 'thresh']:
    try:
        if spect_param_name == 'freq_cutoffs':
            freq_cutoffs = [float(element)
                            for element in
                            config['SPECTROGRAM']['freq_cutoffs'].split(',')]
            spect_params['freq_cutoffs'] = freq_cutoffs
        elif spect_param_name == 'thresh':
            spect_params['thresh'] = float(config['SPECTROGRAM']['thresh'])

    except NoOptionError:
        logger.info('Parameter for computing spectrogram, {}, not specified. '
                    'Will use default.'.format(spect_param_name))
        continue
if spect_params == {}:
    spect_params = None

print('loading training data')
labelset = list(config['DATA']['labelset'])
train_data_dir = config['DATA']['data_dir']
number_song_files = int(config['DATA']['number_song_files'])
skip_files_with_labels_not_in_labelset = config.getboolean(
    'DATA',
    'skip_files_with_labels_not_in_labelset')
labels_mapping_file = os.path.join(results_dirname, 'labels_mapping')
with open(labels_mapping_file, 'rb') as labels_map_file_obj:
    labels_mapping = pickle.load(labels_map_file_obj)

(train_song_spects,
 train_song_labels,
 timebin_dur,
 putative_cbins_used) = cnn_bilstm.utils.load_data(labelset,
                                                   train_data_dir,
                                                   number_song_files,
                                                   spect_params,
                                                   labels_mapping,
                                                   skip_files_with_labels_not_in_labelset)
train_spects_filename = os.path.join(summary_dirname, 'train_spects')

cbins_used_filename = os.path.join(results_dirname, 'training_cbins_used')
with open(cbins_used_filename, 'rb') as cbins_used_file:
    cbins_used = pickle.load(cbins_used_file)
assert putative_cbins_used == cbins_used

train_spect_dict = {'train_spects': train_song_spects,
                    'train_song_labels': train_song_labels,
                    'labels_mapping': labels_mapping}
joblib.dump(train_spect_dict, train_spects_filename)
scipy.io.savemat(train_spects_filename, train_spect_dict)

# num train songs is different from num train song files
# because we take training and validation data from same training song file directory
num_train_songs = int(config['DATA']['num_train_songs'])

# reshape training data
X_train = np.concatenate(train_song_spects[:num_train_songs], axis=0)
Y_train = np.concatenate(train_song_labels[:num_train_songs], axis=0)
input_vec_size = X_train.shape[-1]

print('loading testing data')
test_data_dir = config['DATA']['test_data_dir']
number_test_song_files = int(config['DATA']['number_test_song_files'])
# below, [:2] because don't need timebin duration or mapping
(test_song_spects,
 test_song_labels) = cnn_bilstm.utils.load_data(labelset,
                                                test_data_dir,
                                                number_test_song_files,
                                                spect_params,
                                                labels_mapping,
                                                skip_files_with_labels_not_in_labelset)[:2]

test_spects_filename = os.path.join(summary_dirname,'test_spects')
joblib.dump(test_song_spects, test_spects_filename)
scipy.io.savemat(test_spects_filename, {'test_spects': test_song_spects,
                                         'test_song_labels': test_song_labels})

# here there's no "validation test set" so we just concatenate all test spects
# from all the files we loaded, unlike with training set
X_test = np.concatenate(test_song_spects, axis=0)
# copy X_test because it gets scaled and reshape in main loop
X_test_copy = np.copy(X_test)
Y_test = np.concatenate(test_song_labels, axis=0)
# also need copy of Y_test
# because it also gets reshaped in loop
# and because we need to compare with Y_pred
Y_test_copy = np.copy(Y_test)

# initialize arrays to hold summary results
Y_pred_test_all = []  # will be a nested list
Y_pred_train_all = [] # will be a nested list
train_err_arr = np.empty((len(TRAIN_SET_DURS), len(REPLICATES)))
test_err_arr = np.empty((len(TRAIN_SET_DURS), len(REPLICATES)))

for dur_ind, train_set_dur in enumerate(TRAIN_SET_DURS):

    Y_pred_test_this_dur = []
    Y_pred_train_this_dur = []

    for rep_ind, replicate in enumerate(REPLICATES):
        print("getting train and test error for "
              "training set with duration of {} seconds, "
              "replicate {}".format(train_set_dur, replicate))
        training_records_dir = os.path.join(results_dirname,
                                            ('records_for_training_set_with_duration_of_'
                                             + str(train_set_dur) + '_sec_replicate_'
                                             + str(replicate))
                                            )
        checkpoint_filename = ('checkpoint_train_set_dur_'
                               + str(train_set_dur) +
                               '_sec_replicate_'
                               + str(replicate))

        train_inds_file = glob(os.path.join(training_records_dir, 'train_inds'))[0]
        with open(os.path.join(train_inds_file), 'rb') as train_inds_file:
            train_inds = pickle.load(train_inds_file)

        # get training set
        X_train_subset = X_train[train_inds, :]
        Y_train_subset = Y_train[train_inds]
        # normalize before reshaping to avoid even more convoluted array reshaping
        if normalize_spectrograms:
            scaler_name = ('spect_scaler_duration_{}_replicate_{}'
                           .format(train_set_dur, replicate))
            spect_scaler = joblib.load(os.path.join(results_dirname, scaler_name))
            X_train_subset = spect_scaler.transform(X_train_subset)
            X_test = spect_scaler.transform(X_test_copy)
            Y_test = np.copy(Y_test_copy)

        scaled_data_filename = os.path.join(summary_dirname,
                                            'scaled_spects_duration_{}_replicate_{}'
                                            .format(train_set_dur, replicate))
        scaled_data_dict = {'X_train_subset_scaled': X_train_subset,
                            'X_test_scaled': X_test}
        joblib.dump(scaled_data_dict, scaled_data_filename)
        scipy.io.savemat(scaled_data_filename, scaled_data_dict)

        # now that we normalized, we can reshape
        (X_train_subset,
         Y_train_subset,
         num_batches_train) = cnn_bilstm.utils.reshape_data_for_batching(X_train_subset,
                                                                         Y_train_subset,
                                                                         batch_size,
                                                                         time_steps,
                                                                         input_vec_size)
        (X_test,
         Y_test,
         num_batches_test) = cnn_bilstm.utils.reshape_data_for_batching(X_test,
                                                                        Y_test,
                                                                        batch_size,
                                                                        time_steps,
                                                                        input_vec_size)

        scaled_reshaped_data_filename = os.path.join(summary_dirname,
                                            'scaled_reshaped_spects_duration_{}_replicate_{}'
                                            .format(train_set_dur, replicate))
        scaled_reshaped_data_dict = {'X_train_subset_scaled_reshaped': X_train_subset,
                                     'Y_train_subset_reshaped': Y_train_subset,
                                     'X_test_scaled_reshaped': X_test,
                                     'Y_test_reshaped': Y_test}
        joblib.dump(scaled_reshaped_data_dict, scaled_reshaped_data_filename)
        scipy.io.savemat(scaled_reshaped_data_filename, scaled_reshaped_data_dict)

        meta_file = glob(os.path.join(training_records_dir, 'checkpoint*meta*'))[0]
        data_file = glob(os.path.join(training_records_dir, 'checkpoint*data*'))[0]

        with tf.Session(graph=tf.Graph()) as sess:
            tf.logging.set_verbosity(tf.logging.ERROR)
            saver = tf.train.import_meta_graph(meta_file)
            saver.restore(sess, data_file[:-20])

            # Retrieve the Ops we 'remembered'.
            logits = tf.get_collection("logits")[0]
            X = tf.get_collection("specs")[0]
            Y = tf.get_collection("labels")[0]
            lng = tf.get_collection("lng")[0]

            # Add an Op that chooses the top k predictions.
            eval_op = tf.nn.top_k(logits)

            if 'Y_pred_train' in locals():
                del Y_pred_train

            print('calculating training set error')
            for b in range(num_batches_train):  # "b" is "batch number"
                d = {X: X_train_subset[:, b * time_steps: (b + 1) * time_steps, :],
                     Y: Y_train_subset[:, b * time_steps: (b + 1) * time_steps],
                     lng: [time_steps] * batch_size}

                if 'Y_pred_train' in locals():
                    preds = sess.run(eval_op, feed_dict=d)[1]
                    preds = preds.reshape(batch_size, -1)
                    Y_pred_train = np.concatenate((Y_pred_train, preds), axis=1)
                else:
                    Y_pred_train = sess.run(eval_op, feed_dict=d)[1]
                    Y_pred_train = Y_pred_train.reshape(batch_size, -1)

            Y_train_arr = Y_train[train_inds]
            # get rid of predictions to zero padding that don't matter
            Y_pred_train = Y_pred_train.ravel()[:Y_train_arr.shape[0], np.newaxis]
            train_err = np.sum(Y_pred_train - Y_train_arr != 0) / Y_train_arr.shape[0]
            train_err_arr[dur_ind, rep_ind] = train_err
            print('train error was {}'.format(train_err))
            Y_pred_train_this_dur.append(Y_pred_train)

            if 'Y_pred_test' in locals():
                del Y_pred_test

            print('calculating test set error')
            for b in range(num_batches_test):  # "b" is "batch number"
                d = {X: X_test[:, b * time_steps: (b + 1) * time_steps, :],
                     Y: Y_test[:, b * time_steps: (b + 1) * time_steps],
                     lng: [time_steps] * batch_size}

                if 'Y_pred_test' in locals():
                    preds = sess.run(eval_op, feed_dict=d)[1]
                    preds = preds.reshape(batch_size, -1)
                    Y_pred_test = np.concatenate((Y_pred_test, preds), axis=1)
                else:
                    Y_pred_test = sess.run(eval_op, feed_dict=d)[1]
                    Y_pred_test = Y_pred_test.reshape(batch_size, -1)

            # again get rid of zero padding predictions
            Y_pred_test = Y_pred_test.ravel()[:Y_test_copy.shape[0], np.newaxis]
            test_err = np.sum(Y_pred_test - Y_test_copy != 0) / Y_test_copy.shape[0]
            test_err_arr[dur_ind, rep_ind] = test_err
            print('test error was {}'.format(test_err))
            Y_pred_test_this_dur.append(Y_pred_test)

    Y_pred_train_all.append(Y_pred_train_this_dur)
    Y_pred_test_all.append(Y_pred_test_this_dur)

Y_pred_train_filename = os.path.join(summary_dirname,
                                  'Y_pred_train_all')
with open(Y_pred_train_filename,'wb') as Y_pred_train_file:
    pickle.dump(Y_pred_train_all, Y_pred_train_file)

Y_pred_test_filename = os.path.join(summary_dirname,
                                  'Y_pred_test_all')
with open(Y_pred_test_filename,'wb') as Y_pred_test_file:
    pickle.dump(Y_pred_test_all, Y_pred_test_file)

train_err_filename = os.path.join(summary_dirname,
                                  'train_err')
with open(train_err_filename,'wb') as train_err_file:
    pickle.dump(train_err_arr, train_err_file)

test_err_filename = os.path.join(summary_dirname,
                                  'test_err')
with open(test_err_filename, 'wb') as test_err_file:
    pickle.dump(test_err_arr, test_err_file)

pred_and_err_dict = {'Y_pred_train_all': Y_pred_train_all,
                     'Y_pred_test_all': Y_pred_test_all,
                     'train_err': train_err,
                     'test_err': test_err}

pred_err_dict_filename = os.path.join(summary_dirname,
                                      'y_preds_and_err_for_train_and_test')
scipy.io.savemat(pred_err_dict_filename, pred_and_err_dict)