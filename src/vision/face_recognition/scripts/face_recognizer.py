#!/usr/bin/env python2
#
# Copyright 2015-2016 Carnegie Mellon University
# Copyright 2016 Hanson Robotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import cv2
import pickle
import random
import uuid
import datetime as dt
import time
import numpy as np
import pandas as pd
import logging
import multiprocessing
import shutil
import tempfile
from collections import deque

from sklearn.preprocessing import LabelEncoder
from sklearn.svm import SVC
import dlib
import openface
from openface.data import iterImgs
import rospy
from cv_bridge import CvBridge
from sensor_msgs.msg import Image
from dynamic_reconfigure.server import Server
from face_recognition.cfg import FaceRecognitionConfig

CWD = os.path.dirname(os.path.abspath(__file__))
HR_MODELS = os.environ.get('HR_MODELS', os.path.expanduser('~/.hr/cache/models'))
ARCHIVE_DIR = os.path.expanduser('~/.hr/faces')
DLIB_FACEPREDICTOR = os.path.join(HR_MODELS,
                    'shape_predictor_68_face_landmarks.dat')
NETWORK_MODEL = os.path.join(HR_MODELS, 'nn4.small2.v1.t7')
CLASSIFIER_DIR = os.path.join(HR_MODELS, 'classifier')
logger = logging.getLogger('hr.vision.face_recognition.face_recognizer')

class FaceRecognizer(object):

    def __init__(self):
        self.bridge = CvBridge()
        self.imgDim = 96
        self.align = openface.AlignDlib(DLIB_FACEPREDICTOR)
        self.net = openface.TorchNeuralNet(NETWORK_MODEL, self.imgDim)
        self.landmarkIndices = openface.AlignDlib.OUTER_EYES_AND_NOSE
        self.face_detector = dlib.get_frontal_face_detector()
        self.count = 0
        self.train = False
        self.enable = True
        self.data_root = 'faces'
        self.train_dir = os.path.join(self.data_root, 'training-images')
        self.aligned_dir = os.path.join(self.data_root, 'aligned-images')
        self.classifier_dir = CLASSIFIER_DIR
        self.clf, self.le = None, None
        self.load_classifier(os.path.join(self.classifier_dir, 'classifier.pkl'))
        self.ros_name = rospy.get_name()
        self.multi_faces = False
        self.threshold = 0
        self.detected_faces = deque(maxlen=10)

    def load_classifier(self, model):
        if os.path.isfile(model):
            with open(model) as f:
                self.le, self.clf = pickle.load(f)
                logger.info("Loaded model {}".format(model))
        else:
            logger.error("Model file {} is not found".format(model))

    def getRep(self, bgrImg, all=True):
        if bgrImg is None:
            return []

        rgbImg = cv2.cvtColor(bgrImg, cv2.COLOR_BGR2RGB)
        if all:
            bb = self.align.getAllFaceBoundingBoxes(rgbImg)
        else:
            bb = self.align.getLargestFaceBoundingBox(rgbImg)

        if bb is None:
            return []

        if not hasattr(bb, '__iter__'):
            bb = [bb]

        alignedFaces = []
        for box in bb:
            alignedFaces.append(
                self.align.align(
                    self.imgDim,
                    rgbImg,
                    box,
                    landmarkIndices=self.landmarkIndices))

        reps = []
        for alignedFace in alignedFaces:
            reps.append(self.net.forward(alignedFace))

        return reps

    def align_images(self, input_dir):
        imgs = list(iterImgs(input_dir))
        # Shuffle so multiple versions can be run at once.
        random.shuffle(imgs)

        for imgObject in imgs:
            outDir = os.path.join(self.aligned_dir, imgObject.cls)
            if not os.path.isdir(outDir):
                os.makedirs(outDir)
            outputPrefix = os.path.join(outDir, imgObject.name)
            imgName = outputPrefix + ".png"

            if not os.path.isfile(imgName):
                rgb = imgObject.getRGB()
                if rgb is None:
                    outRgb = None
                else:
                    outRgb = self.align.align(
                            self.imgDim, rgb,
                            landmarkIndices=self.landmarkIndices)
                if outRgb is not None:
                    outBgr = cv2.cvtColor(outRgb, cv2.COLOR_RGB2BGR)
                    cv2.imwrite(imgName, outBgr)
                    logger.info("Write image {}".format(imgName))
                else:
                    os.remove(imgObject.path)
                    logger.warn("No face was detected in {}. Removed.".format(imgObject.path))

    def gen_data(self):
        face_reps = []
        labels = []
        reps_fname = "{}/reps.csv".format(self.aligned_dir)
        label_fname = "{}/labels.csv".format(self.aligned_dir)
        for imgObject in iterImgs(self.aligned_dir):
            reps = self.net.forward(imgObject.getRGB())
            face_reps.append(reps)
            labels.append((imgObject.cls, imgObject.path))
        if face_reps and labels:
            pd.DataFrame(face_reps).to_csv(reps_fname, header=False, index=False)
            pd.DataFrame(labels).to_csv(label_fname, header=False, index=False)
            logger.info("Generated label file {}".format(label_fname))
            logger.info("Generated representation file {}".format(reps_fname))

    def collect_face(self, image, crop=False):
        img_dir = os.path.join(self.train_dir, self.face_name)
        if not os.path.isdir(img_dir):
            os.makedirs(img_dir)
        detected_faces = self.face_detector(image)
        if detected_faces:
            face = max(detected_faces, key=lambda rect: rect.width() * rect.height())
            if crop:
                image = image[face.top():face.bottom(), face.left():face.right()]
                if image.size == 0:
                    return
            fname = os.path.join(img_dir, "{}.jpg".format(uuid.uuid1().hex))
            cv2.imwrite(fname, image)
            logger.info("Write face image to {}".format(fname))
            print "Write face image to {}".format(fname)

    def prepare(self):
        """Align faces, generate representations and labels"""
        self.align_images(self.train_dir)
        self.gen_data()

    def train_model(self):
        self.prepare()
        label_fname = "{}/labels.csv".format(self.aligned_dir)
        reps_fname = "{}/reps.csv".format(self.aligned_dir)
        labels, embeddings = None, None
        if os.path.isfile(label_fname) and \
                    os.path.isfile(reps_fname):
            labels = pd.read_csv(label_fname, header=None)
            embeddings = pd.read_csv(reps_fname, header=None)

        if labels is None or embeddings is None:
            logger.error("No labels or representations are found")
            return

        # append the existing data
        original_label_fname = "{}/labels.csv".format(self.classifier_dir)
        original_reps_fname = "{}/reps.csv".format(self.classifier_dir)
        if os.path.isfile(original_label_fname) and \
                    os.path.isfile(original_reps_fname):
            labels2 = pd.read_csv(original_label_fname, header=None)
            embeddings2 = pd.read_csv(original_reps_fname, header=None)
            labels = labels.append(labels2)
            embeddings = embeddings.append(embeddings2)

        labels_data = labels.as_matrix()[:,0].tolist()
        embeddings_data = embeddings.as_matrix()

        le = LabelEncoder().fit(labels_data)
        labelsNum = le.transform(labels_data)
        clf = SVC(C=1, kernel='linear', probability=True)
        clf.fit(embeddings_data, labelsNum)

        classifier_fname = "{}/classifier.pkl".format(self.aligned_dir)
        with open(classifier_fname, 'w') as f:
            pickle.dump((le, clf), f)
        logger.info("Model saved to {}".format(classifier_fname))
        self.load_classifier(classifier_fname)

        labels.to_csv(label_fname, header=False, index=False)
        embeddings.to_csv(reps_fname, header=False, index=False)
        logger.info("Update label file {}".format(label_fname))
        logger.info("Update representation file {}".format(reps_fname))

    def infer(self, img):
        if self.clf is None or self.le is None:
            return None, None
        reps = self.getRep(img, self.multi_faces)
        persons = []
        confidences = []
        for rep in reps:
            try:
                rep = rep.reshape(1, -1)
            except:
                logger.info("No Face detected")
                return (None, None)
            predictions = self.clf.predict_proba(rep).ravel()
            maxI = np.argmax(predictions)
            persons.append(self.le.inverse_transform(maxI))
            confidences.append(predictions[maxI])
        return persons, confidences

    def image_cb(self, ros_image):
        if not self.enable:
            return

        self.count += 1
        if self.count % 30 != 0:
            return
        image = self.bridge.imgmsg_to_cv2(ros_image, "bgr8")
        if self.train:
            self.collect_face(image)
        else:
            persons, confidences = self.infer(image)
            if persons:
                for p, c in zip(persons, confidences):
                    if c <= self.threshold:
                        continue
                    self.detected_faces.append(p)
                    logger.info("P: {} C: {}".format(p, c))
                    print "P: {} C: {}".format(p, c)
                rospy.set_param('{}/recent_persons'.format(self.ros_name),
                            ','.join(self.detected_faces))

    def reset(self):
        archive_fname = os.path.join(ARCHIVE_DIR, 'faces-{}'.format(
                dt.datetime.strftime(dt.datetime.now(), '%Y%m%d%H%M%S')))
        shutil.make_archive(archive_fname, 'gztar', root_dir=CWD, base_dir='faces')
        shutil.rmtree(self.train_dir, ignore_errors=True)
        shutil.rmtree(self.aligned_dir, ignore_errors=True)
        self.load_classifier(os.path.join(self.classifier_dir, 'classifier.pkl'))

    def reconfig(self, config, level):
        self.enable = config.enable
        if not self.enable:
            config.reset = False
            config.train = False
            return config
        if self.train and not config.train:
            try:
                self.train_model()
            except Exception as ex:
                logger.error("Train model failed")
                logger.error(ex)
        self.train = config.train
        self.face_name = config.face_name
        self.threshold = config.confidence_threshold
        self.multi_faces = config.multi_faces
        if config.reset:
            config.train = False
            self.train = False
            time.sleep(0.2)
            try:
                self.reset()
            except Exception as ex:
                logger.error(ex)
            config.reset = False
        return config

if __name__ == '__main__':
    rospy.init_node("face_recognizer")
    recognizer = FaceRecognizer()
    Server(FaceRecognitionConfig, recognizer.reconfig)
    rospy.Subscriber('/camera/image_raw', Image, recognizer.image_cb)
    rospy.spin()

    #logging.basicConfig()
    #logging.getLogger().setLevel(logging.INFO)
    #recognizer = FaceRecognizer()
    #recognizer.train_model()
    #
