import cv2
import tensorflow as tf
from tensorflow.python.layers.core import dense, dropout, flatten
from tensorflow.python.layers.convolutional import conv2d, conv2d_transpose
from tensorflow.contrib.layers.python.layers.initializers import xavier_initializer as xavier
from tensorflow.contrib.tensorboard.plugins import projector
from tqdm import trange, tqdm
import numpy as np
import os
import sys
sys.path.append('..')
#from show_graph import show_graph
from metrics import Metrics
from utils import save_images

class Model:
    def __init__(self, in_shape, lr=0.001, embedding_dim=50, num_projections=20):
        '''
        classes:  List of class names or integers corrisponding to each class being classified
                  by the network. ie: ['left', 'straight', 'right'] or [0, 1, 2]
        '''
        # number of elements in the embedding vector
        self.embedding_dim = embedding_dim
        # number of test embeddings to project in tensorboard
        self.num_projections = num_projections
        # Define model
        tf.reset_default_graph()
        self.x = tf.placeholder(tf.float32, shape=[None,]+in_shape, name="x")
        self.y = tf.placeholder(tf.float32, shape=[None,]+in_shape, name="y")

        self._training    = tf.placeholder(tf.bool)
        self.training     = tf.get_variable("training", dtype=tf.bool,
                                             initializer=True,
                                             trainable=False)
        self.set_training = self.training.assign(self._training)

        self._beta        = tf.placeholder(dtype=tf.float32)
        self.beta         = tf.get_variable("beta", dtype=tf.float32,
                                             initializer=0.,
                                             trainable=False)
        self.update_beta  = self.beta.assign(self._beta)

        self._embeddings  = tf.placeholder(tf.float32,
                                           shape=[None, self.embedding_dim])
        self.embeddings   = tf.get_variable("embeddings",
                                             dtype=tf.float32,
                                             shape=
                                            [self.num_projections,
                                             self.embedding_dim],
                                             initializer=tf.zeros_initializer(),
                                             trainable=False)
        self.update_embeddings = self.embeddings.assign(self._embeddings)

        paddings = tf.constant([[0,0],[4,4],[0,0],[0,0]])
        relu    = tf.nn.relu
        sigmoid = tf.nn.sigmoid
        with tf.name_scope("encoder"):
            # Padding invector so reconstruction returns to the correct size.
            x_padded = tf.pad(self.x, paddings, "SYMMETRIC")
            # Encoder            in     num   shape  stride   pad
            enc1  = conv2d(x_padded,  24,  (5,5), (2,2),  "same",
                           activation=relu, kernel_initializer=xavier(),
                           name="enc1")# 64, 80,24
            enc2  = conv2d(enc1, 32,  (5,5), (2,2),  "same",
                           activation=relu, kernel_initializer=xavier(),
                           name="enc2")# 32, 40,32
            enc3  = conv2d(enc2, 64,  (5,5), (2,2),  "same",
                           activation=relu, kernel_initializer=xavier(),
                           name="enc3")# 16, 20,64
            enc4  = conv2d(enc3, 64,  (3,3), (2,2),  "same",
                           activation=relu, kernel_initializer=xavier(),
                           name="enc4")#  8, 10,64
            enc5  = conv2d(enc4, 64,  (3,3), (1,1),  "same",
                           activation=relu, kernel_initializer=xavier(),
                           name="enc5")#  8, 10,64
            enc5f = flatten(enc5)
            #                     in   num
            enc6  = dense(enc5f, 100,
                          activation=relu, kernel_initializer=xavier(),
                          name="enc6")
            enc6d = dropout(enc6, rate=0.1, training=self.training, name="enc6d")

        with tf.name_scope("sampling"):
            # VAE sampling
            '''
            Note: exp(log(log_sigma / 2)) = sigma
            '''
            self.mu           = dense(enc6d, self.embedding_dim, activation=None,
                                      kernel_initializer=xavier(),
                                      name="mu")
            self.log_sigma = dense(enc6d, self.embedding_dim, activation=None,
                                      kernel_initializer=xavier(),
                                      name="log_sigma")
            eps          = tf.random_normal(shape=tf.shape(self.mu),
                                            mean=0.0, stddev=1.0,
                                            dtype=tf.float32, name="eps")
            self.noisy_sigma  = tf.exp(self.log_sigma) * eps
            self.z       = tf.add(self.mu, self.noisy_sigma, name="z")

            tf.summary.histogram("z", self.z)
            tf.summary.histogram("log_sigma", self.log_sigma)
            tf.summary.histogram("mu", self.mu)
            tf.summary.histogram("eps", eps)

        with tf.name_scope("decoder"):
            # Decoder    in          num
            dec1 = dense(self.z, 100, activation=relu,
                             kernel_initializer=xavier(), name="dec1")
            dec2 = dense(dec1, (8*10*64), activation=relu,
                             kernel_initializer=xavier(), name="dec2")
            dec2r  = tf.reshape(dec2, (-1,8,10,64))
            #                        in num  shape  stride   pad
            dec3  = conv2d_transpose(dec2r, 64, (3,3), (1,1), "same",
                                         activation=relu, name="dec3")
            dec4  = conv2d_transpose(dec3, 64, (3,3), (2,2), "same",
                                         activation=relu, name="dec4")
            dec5  = conv2d_transpose(dec4, 32, (5,5), (2,2), "same",
                                         activation=relu, name="dec5")
            dec6  = conv2d_transpose(dec5, 24, (5,5), (2,2), "same",
                                         activation=relu, name="dec6")

            dec7  = conv2d_transpose(dec6, 3,  (5,5), (2,2), "same",
                                         activation=None, name="dec8")
            self.dec  = relu(dec7)

        with tf.name_scope("loss"):
            # # VAE Loss
            # 1. Reconstruction loss: How far did we get from the actual image?
            y_padded = tf.pad(self.y, paddings, "SYMMETRIC")
            self.rec_loss = tf.reduce_sum(tf.square(y_padded - self.dec), axis=[1,2,3])

            # 2. KL-Divergence: How far from the distribution 'z' is sampled
            #                   from the desired zero mean unit variance?
            self.kl_loss = 0.5 * tf.reduce_sum(tf.square(self.mu)
                                            + (tf.exp(2*self.log_sigma))
                                            - 2*self.log_sigma
                                            - 1
                                            , axis=1)
            self.loss =  tf.reduce_mean(self.rec_loss + self.kl_loss*self.beta,
                                        name='total_loss')

            tf.summary.scalar("total_loss", self.loss)
            tf.summary.scalar("kl_loss", tf.reduce_mean(self.kl_loss))
            tf.summary.scalar("rec_loss", tf.reduce_mean(self.rec_loss))
            tf.summary.scalar("beta", self.beta)

        optimizer = tf.train.AdamOptimizer(learning_rate=lr)
        self.train_step = optimizer.minimize(self.loss, name="train_step")

        self.init_vars = tf.global_variables_initializer()
        self.saver = tf.train.Saver()

    def setup_meta(self, save_dir, test_gen):
        logs_path = os.path.join(save_dir, "train_logs")
        metadata  = os.path.join(logs_path, "metadata.tsv")
        if not os.path.exists(logs_path):
            os.makedirs(logs_path)
        test_gen.reset(shuffle=False)
        t_test = trange(test_gen.steps_per_epoch)
        t_test.set_description('tf.projector meta')
        labels = []
        for _ in t_test:
            batch  = test_gen.get_next_batch()
            labels.extend([a["steering"] for a in batch["annotations"]])

        with open(metadata, 'w') as meta_file:
            for steering_bin in labels[:self.num_projections]:
                _bin = int(steering_bin)
                meta_file.write("{}\n".format(int(_bin)))

        config = projector.ProjectorConfig()
        embedding = config.embeddings.add()
        embedding.tensor_name = self.embeddings.name
        embedding.metadata_path = metadata
        return logs_path


    def train(self, train_gen, test_gen, save_dir, epochs=10, lr=0.001,
              sample_inf_gen=None, annealing_epochs=0, beta_max=1):

        logs_path = self.setup_meta(save_dir, test_gen)

        # Tensorboard
        merge = tf.summary.merge_all()

        best_ckpt = ""
        with tf.Session() as sess:
            # Tensorboard
            train_writer = tf.summary.FileWriter(logs_path, sess.graph)

            # Init
            sess.run(self.init_vars)

            # some big number
            best_loss = 10**9.0

            # TODO: restart from checkpoint

            global_step = 0.
            for e in range(epochs):

                # Begin Training
                # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                sess.run(self.set_training, feed_dict={self._training: True})
                train_gen.reset()
                t_train = trange(train_gen.steps_per_epoch)
                t_train.set_description(f"Training Epoch: {e+1}")
                for step in t_train:
                    _beta = self.anneal_beta(global_step, annealing_epochs,
                                            train_gen.steps_per_epoch, beta_max)
                    sess.run(self.update_beta, feed_dict={self._beta: _beta})
                    batch = train_gen.get_next_batch()
                    summary, _, loss, b = sess.run([merge, self.train_step,
                                                 self.loss, self.beta],
                               feed_dict={self.x: batch["augmented_images"],
                                          self.y: batch["original_images"],
                                          self.training: True})
                    train_writer.add_summary(summary, global_step)
                    global_step += 1
                    print(f"beta: {b}")

                # Begin Testing
                #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                sess.run(self.set_training, feed_dict={self._training: False})
                embeddings      = []
                test_gen.reset(shuffle=False)
                t_test = trange(test_gen.steps_per_epoch)
                t_test.set_description('Testing')
                for _ in t_test:
                    batch = test_gen.get_next_batch()
                    loss, zeds = sess.run([self.loss, self.z],
                               feed_dict={self.x: batch["augmented_images"],
                                          self.y: batch["original_images"],
                                          self.training: False})
                    embeddings.extend(zeds)
                # House keeping
                #~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

                # Update embedding being plotted by tensorboard
                sess.run(self.update_embeddings,
                         feed_dict={self._embeddings:
                                    embeddings[:self.num_projections]})

                # Only begin saving checkpoints after beta is fully annealed
                if e >= annealing_epochs:
                    # only need to save graph once, then save the weights
                    # at each improving epoch
                    if e == annealing_epochs:
                        path = os.path.join(save_dir, "model_def")
                        self.saver.export_meta_graph(path + ".meta")
                    # If we have a new best loss then save the ckpt and some
                    # sample inferences
                    cur_loss = np.mean(loss)
                    print(f"Test Loss: {cur_loss:0.3f}\n" + 50*"-")
                    if cur_loss < best_loss:
                            best_loss = cur_loss
                            path = f"{save_dir}/ep_{e+1}_loss_{best_loss:0.3f}"
                            best_ckpt = self.saver.save(sess, path + ".ckpt",
                                                        write_meta_graph=False)
                            print("Model saved at {}".format(best_ckpt))
                            if (sample_inf_gen is not None):
                                path = os.path.join(*[save_dir,
                                               "sample_inferences", f"ep_{e}"])
                                self.save_sample_inference(sess,
                                                          sample_inf_gen, path)

        print(f"Done, final best loss: {best_loss:0.3f}")
        return {"best_ckpt": best_ckpt, "best_loss": best_loss}


    def anneal_beta(self, step, annealing_epochs, steps_per_epoch, beta_max):
        if annealing_epochs==0: return beta_max
        return min(1.0, step/(annealing_epochs*steps_per_epoch)) * beta_max

    def save_sample_inference(self, sess, sample_inf_gen, save_dir):
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)

        batch = sample_inf_gen.get_next_batch()
        sample_inf_gen.reset(shuffle=False)
        reconstructions = sess.run(self.dec,
                                   feed_dict={self.x: batch["original_images"],
                                              self.training: False})
        names = [a["image"] for a in batch["annotations"]]
        save_images(reconstructions, names, save_dir)
