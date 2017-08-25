import os
import shutil
import argparse

import tensorflow as tf

from multi_mnist import read_and_decode
from multi_mnist import read_test_data

from air_model import AIRModel


EPOCHS = 300
BATCH_SIZE = 64
CANVAS_SIZE = 50

NUM_SUMMARIES_EACH_ITERATIONS = 50
VAR_SUMMARIES_EACH_ITERATIONS = 500
IMG_SUMMARIES_EACH_ITERATIONS = 1000
GRAD_SUMMARIES_EACH_ITERATIONS = 100
SAVE_PARAMS_EACH_ITERATIONS = 10000
NUM_IMAGES_TO_SAVE = 60

DEFAULT_READER_THREADS = 4
DEFAULT_RESULTS_FOLDER = "air_results"
TRAIN_DATA_FILE = "multi_mnist_data/common.tfrecords"
TEST_DATA_FILE = "multi_mnist_data/test.tfrecords"


# parsing command-line arguments
parser = argparse.ArgumentParser()
parser.add_argument("-r", "--results-folder", default=DEFAULT_RESULTS_FOLDER)
parser.add_argument("-o", "--overwrite-results", type=int, choices=[0, 1], default=0)
parser.add_argument("-t", "--reader-threads", type=int, default=DEFAULT_READER_THREADS)
args = parser.parse_args()

MODELS_FOLDER = args.results_folder + "/models/"
SUMMARIES_FOLDER = args.results_folder + "/summary/"
SOURCE_FOLDER = args.results_folder + "/source/"


# removing existing results folder (with content), if configured so
# otherwise, throwing an exception if the results folder exists
if args.overwrite_results:
    shutil.rmtree(args.results_folder, ignore_errors=True)
elif os.path.exists(args.results_folder):
    raise Exception("The folder \"{0}\" already exists".format(
        args.results_folder
    ))

# creating result directories
os.makedirs(args.results_folder)
os.makedirs(MODELS_FOLDER)
os.makedirs(SUMMARIES_FOLDER)
os.makedirs(SOURCE_FOLDER)

# creating a copy of the current version of *.py source files
for file in [f for f in os.listdir(".") if f.endswith(".py")]:
        shutil.copy(file, SOURCE_FOLDER + file)


with tf.variable_scope("pipeline"):
    # fetching a batch of numbers of digits and images from a queue
    filename_queue = tf.train.string_input_producer(
        [TRAIN_DATA_FILE], num_epochs=EPOCHS
    )
    train_data, train_targets = read_and_decode(
        filename_queue, BATCH_SIZE, CANVAS_SIZE, args.reader_threads
    )

    # placeholders for feeding the same test dataset to test model
    test_data = tf.placeholder(tf.float32, shape=[None, CANVAS_SIZE ** 2])
    test_targets = tf.placeholder(tf.int32, shape=[None])

models = []
model_inputs = [
    [train_data, train_targets],
    [test_data, test_targets]
]

# creating two separate models - for training and testing - with
# identical configuration and sharing the same set of variables
for i in range(2):
    models.append(
        AIRModel(
            model_inputs[i][0], model_inputs[i][1],
            max_steps=3, max_digits=2, lstm_units=256, canvas_size=CANVAS_SIZE, windows_size=28,
            vae_latent_dimensions=50, vae_recognition_units=(512, 256), vae_generative_units=(256, 512),
            scale_prior_mean=-1.0, scale_prior_variance=0.1, shift_prior_mean=0.0, shift_prior_variance=1.0,
            vae_prior_mean=0.0, vae_prior_variance=1.0, vae_likelihood_std=0.3,
            z_pres_prior=1e-1, gumbel_temperature=10.0, learning_rate=1e-3, gradient_clipping_norm=100.0,
            num_summary_images=NUM_IMAGES_TO_SAVE, train=(i == 0), reuse=(i == 1), scope="air",
            annealing_schedules={
                "z_pres_prior": {"init": 1e-1, "min": 1e-9, "factor": 0.5, "iters": 1000},
                "gumbel_temperature": {"init": 10.0, "min": 0.1, "factor": 0.8, "iters": 1000},
                "learning_rate": {"init": 1e-3, "min": 1e-4, "factor": 0.5, "iters": 1000}
            }
        )
    )

train_model, test_model = models


config = tf.ConfigProto()
config.gpu_options.allow_growth = True

with tf.Session(config=config) as sess:
    coord = tf.train.Coordinator()

    sess.run(tf.local_variables_initializer())
    sess.run(tf.global_variables_initializer())

    threads = tf.train.start_queue_runners(sess=sess, coord=coord)
    writer = tf.summary.FileWriter(SUMMARIES_FOLDER, sess.graph)
    saver = tf.train.Saver(max_to_keep=10000)

    # diagnostic summaries are fetched from the test model
    num_summaries = tf.summary.merge(test_model.num_summaries)
    var_summaries = tf.summary.merge(test_model.var_summaries)
    img_summaries = tf.summary.merge(test_model.img_summaries)

    # gradient summaries are fetched from the training model
    grad_summaries = tf.summary.merge(train_model.grad_summaries)

    # reading the test dataset, to be used with test model for
    # computing all summaries throughout the training process
    test_images, test_num_digits = read_test_data(TEST_DATA_FILE)

    try:
        # beginning with step = 0 to capture all summaries
        # and save the initial values of the model parameters
        # before the actual training process has started
        step = 0

        while True:
            # saving summaries with configured frequency:
            # it is assumed that frequencies of more rare
            # summaries are divisible by more frequent ones
            if step % NUM_SUMMARIES_EACH_ITERATIONS == 0:
                if step % VAR_SUMMARIES_EACH_ITERATIONS == 0:
                    if step % IMG_SUMMARIES_EACH_ITERATIONS == 0:
                        num_sum, var_sum, img_sum = sess.run(
                            [num_summaries, var_summaries, img_summaries],
                            feed_dict={
                                test_data: test_images,
                                test_targets: test_num_digits
                            }
                        )

                        writer.add_summary(img_sum, step)
                    else:
                        num_sum, var_sum = sess.run(
                            [num_summaries, var_summaries],
                            feed_dict={
                                test_data: test_images,
                                test_targets: test_num_digits
                            }
                        )

                    writer.add_summary(var_sum, step)
                else:
                    num_sum = sess.run(
                        num_summaries,
                        feed_dict={
                            test_data: test_images,
                            test_targets: test_num_digits
                        }
                    )

                writer.add_summary(num_sum, step)

            # saving parameters with configured frequency
            if step % SAVE_PARAMS_EACH_ITERATIONS == 0:
                saver.save(
                    sess, MODELS_FOLDER + "air-model",
                    global_step=step
                )

            # training step
            if step % GRAD_SUMMARIES_EACH_ITERATIONS == 0:
                # with gradient summaries
                _, loss, accuracy, step, grad_sum = sess.run([
                    train_model.training, train_model.loss,
                    train_model.accuracy, train_model.global_step,
                    grad_summaries
                ])

                writer.add_summary(grad_sum, step)
            else:
                # without gradient summaries
                _, loss, accuracy, step = sess.run([
                    train_model.training, train_model.loss,
                    train_model.accuracy, train_model.global_step
                ])

            print("iteration {}\tloss {:.3f}\taccuracy {:.2f}".format(step, loss, accuracy))

    except tf.errors.OutOfRangeError:
        print()
        print("training has ended")
        print()

    finally:
        coord.request_stop()
        coord.join(threads)
