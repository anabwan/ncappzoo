#! /usr/bin/env python3

# Copyright(c) 2019 Intel Corporation.
# License: MIT See LICENSE file in root directory.

from __future__ import print_function
from argparse import ArgumentParser, SUPPRESS
from time import time
from time import sleep
from openvino.inference_engine import IENetwork, IEPlugin
from car_pi import DriveBrickPi3, CAR_DIRECTION
from maps import classes_color_map, classes_traffic_light_map
from lane_detector import LaneDetector, select_region
import cv2
import sys
import os
import numpy as np
import logging as log

# Setting camera resolution size
CAMERA_WIDTH = 896
CAMERA_HEIGHT = 512


def build_argparser():
    parser = ArgumentParser(add_help=False)
    args = parser.add_argument_group('Options')
    args.add_argument('-h', '--help', action='help', default=SUPPRESS, help='Show this help message and exit.')
    args.add_argument("-vr", "--verify_road", action="store_true", help="Optional - Verify road by detecting road "
                                                                        "segmentation this option will take few seconds "
                                                                        "when starting the program, road checked once!")
    args.add_argument("-md", "--manual_driving", action="store_true", help="Use: a, w, d, s, x for manual driving")
    args.add_argument("-s", "--show_frame", action="store_true", help="Whether or not to display frame to screen")
    args.add_argument("-m", "--mirror", action="store_true", help="Flip camera")
    args.add_argument("-fps", "--show_fps", action="store_true", help="Show fps information on top of camera view")
    return parser


def main():
    log.basicConfig(format="[ %(levelname)s ] %(message)s", level=log.INFO, stream=sys.stdout)
    args = build_argparser().parse_args()
    log.info("Manual driving mode is ON - use w, a ,d ,s and x to stop") if args.manual_driving else None

    frames_path = os.path.join(os.path.dirname(__file__), 'src', 'frames')
    road_model_xml = os.getcwd() + "/src/data/road-segmentation/road-segmentation-adas-0001.xml"
    road_model_bin = os.path.splitext(road_model_xml)[0] + ".bin"

    tl_model_xml = os.getcwd() + "/src/data/traffic-light/traffic-light-0001.xml"
    tl_model_bin = os.path.splitext(tl_model_xml)[0] + ".bin"

    device = "MYRIAD"
    fps = ""
    frame_num = 0
    camera_id = 0

    cap = cv2.VideoCapture(camera_id)
    log.info("Loading Camera id {}".format(camera_id))

    cap.set(cv2.CAP_PROP_FPS, 30)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    log.info("Camera size {}x{}".format(CAMERA_WIDTH, CAMERA_HEIGHT))

    log.info("Setting device: {}".format(device))
    plugin = IEPlugin(device=device)

    # Read IR
    tl_net = IENetwork(model=tl_model_xml, weights=tl_model_bin)
    log.info("Traffic-Light network has been loaded:\n\t{}\n\t{}".format(tl_model_xml, tl_model_bin))

    # Open video capture for recognizing hte road
    assert cap.isOpened(), "Couldn't open Camera"
    success, frame = cap.read()
    assert success, "Can't snap image"

    """
    Verify road-segmentation start here
    """
    if args.verify_road:
        road_net = IENetwork(model=road_model_xml, weights=road_model_bin)
        log.info("Road-Segmentation network has been loaded:\n\t{}\n\t{}".format(road_model_xml, road_model_bin))
        assert len(road_net.inputs.keys()) == 1, "Sample supports only single input topologies"
        assert len(road_net.outputs) == 1, "Sample supports only single output topologies"
        log.info("Preparing input blobs")
        input_blob = next(iter(road_net.inputs))
        out_blob = next(iter(road_net.outputs))
        road_net.batch_size = 1
        log.info("Batch size is {}".format(road_net.batch_size))

        # Read and pre-process input images
        n, c, h, w = road_net.inputs[input_blob].shape
        images = np.ndarray(shape=(n, c, h, w))

        if frame.shape[:-1] != (h, w):
            log.warning("Image {} is resized from {} to {}".format("CAM", frame.shape[:-1], (h, w)))
            image = cv2.resize(frame, (w, h))

        image = image.transpose((2, 0, 1))  # Change data layout from HWC to CHW
        images[frame_num] = image
        log.info("Snapping frame: {}".format(frame_num))
        frame_num += 1

        # Loading model to the plugin
        log.info("Loading road-segmentation model to the plugin")
        road_exec_net = plugin.load(network=road_net)

        # Start sync inference
        log.info("Starting inference ({} iterations)".format(1))
        infer_time = []
        for i in range(1):
            t0 = time()
            res = road_exec_net.infer(inputs={input_blob: images})
            infer_time.append((time() - t0) * 1000)
        log.info("Average running time of one iteration: {} ms".format(np.average(np.asarray(infer_time))))

        # Processing output blob
        log.info("Processing output blob")
        res = res[out_blob]
        _, _, out_h, out_w = res.shape
        t0 = time() 
        for batch, data in enumerate(res):
            classes_map = np.zeros(shape=(out_h, out_w, 3), dtype=np.int)
            for i in range(out_h):
                for j in range(out_w):
                    if len(data[:, i, j]) == 1:
                        pixel_class = int(data[:, i, j])
                    else:
                        pixel_class = np.argmax(data[:, i, j])
                    classes_map[i, j, :] = classes_color_map[min(pixel_class, 20)]

        # Check red color (road) percentage - for verifying road
        RED_MIN = np.array([0, 0, 128])
        RED_MAX = np.array([250, 250, 255])
        classes_map = select_region(classes_map)
        out_img = os.path.join(frames_path, "processed_image.bmp")
        cv2.imwrite(out_img, classes_map)
        log.info("Result image was saved to {}".format(out_img))        

        size = classes_map.size
        dstr = cv2.inRange(classes_map, RED_MIN, RED_MAX)
        no_red = cv2.countNonZero(dstr)
        frac_red = np.divide(float(no_red), int(size))
        percent_red = int(np.multiply(frac_red, 100)) + 50  # 50 = black region
        
        log.info("Road-segmentation processing time is: {} sec.".format((time() - t0) * 1000))
        log.info("Road detected {}% of the frame: ".format(str(percent_red)))
        
        if percent_red < 50:
            raise Exception("Can't detect any road!! please put the car on a road")

    """
    Main function start here - detecting road and traffic light
    """
    tl_input_blob = next(iter(tl_net.inputs))
    tl_out_blob = next(iter(tl_net.outputs))

    # Loading model to the plugin
    log.info("Loading traffic-light model to the plugin")
    tl_exec_net = plugin.load(network=tl_net)

    def releaseAll():
        """
        Reset camera video, car and close all opened windows.
        This could cause when stop the program or when something went wrong.
        """
        cap.release()
        car.reset()
        cv2.destroyAllWindows()

    # Start running car and video
    try:
        initial_w = cap.get(3)
        initial_h = cap.get(4)
        del_label = 'go'
        frame_count = 0
        stop_on_u_turn_count = 0

        # initialize car
        car = DriveBrickPi3()
        log.info("Car name is {}".format(car.name))
        # initialize road detection - start with first frame
        detector = LaneDetector(frame)

        # Start capturing...
        while cap.isOpened():
            t1 = time()

            success, orig_frame = cap.read()
            assert success, "Can't snap image"

            if args.mirror:
                orig_frame.flip(orig_frame, 1)
                log.info("Using camera mirror")

            # Update image
            detector.image = orig_frame

            # Set configurations for traffic light detection
            prepimg = cv2.resize(orig_frame, (300, 300))
            prepimg = prepimg[np.newaxis, :, :, :]
            prepimg = prepimg.transpose((0, 3, 1, 2))
            tl_outputs = tl_exec_net.infer(inputs={tl_input_blob: prepimg})
            res = tl_exec_net.requests[0].outputs[tl_out_blob]

            detecting_traffic_light = False

            # Search for all detected objects (for traffic light)
            for obj in res[0][0]:
                # Draw only objects when probability more than specified threshold
                confidence = obj[2] * 100
                if 85 < confidence < 100:
                    detecting_traffic_light = True
                    best_proposal = obj

                    xmin = int(best_proposal[3] * initial_w)
                    ymin = int(best_proposal[4] * initial_h)
                    xmax = int(best_proposal[5] * initial_w)
                    ymax = int(best_proposal[6] * initial_h)
                    class_id = int(best_proposal[1])

                    # Make sure camera detecting only 3 classes
                    if class_id <= 2:
                        # Draw box and label\class_id
                        color = (255, 0, 0) if class_id == 1 else (50, 205, 50)
                        cv2.rectangle(orig_frame, (xmin, ymin), (xmax, ymax), color, 2)

                        det_label = classes_traffic_light_map[class_id - 1] if classes_traffic_light_map else str(class_id)
                        label_and_prob = det_label + ", " + str(confidence) + "%"
                        cv2.putText(orig_frame, label_and_prob, (xmin, ymin - 7), cv2.FONT_HERSHEY_COMPLEX, 0.6, color, 1)
                        if str(det_label) == 'go':
                            car.status = CAR_DIRECTION.FWD
                        elif str(del_label) == 'stop':
                            car.status = CAR_DIRECTION.STOP
                        else:
                            car.status = CAR_DIRECTION.STOP

            # Image process - start looking for mid line of the road
            # note that, the following function is looking for the yellow line in the middle
            mid_lines = detector.process()

            if not args.manual_driving:
                # when car status is forward (it means that we didn't see any traffic light or the traffic
                # light is green. and of course street was recognize with yellow middle line.
                if mid_lines is not None and car.status is CAR_DIRECTION.FWD:
                    x1, y1, x2, y2 = mid_lines[0][0]
                    stop_on_u_turn_count = 0

                    cv2.line(orig_frame, (x1, y1), (x2, y2), (0, 180, 0), 5)

                    slope = (y1 - y2) / (x1 - x2) if x1 - x2 != 0 else 50                        
                    log.debug("slope: {}".format(str(slope)))
                    log.debug("x1 {}, x2 {}, y1 {}, y2 {}".format(str(x1), str(x2), str(y1), str(y2)))
                    if slope < 0:
                        # go left
                        log.debug("detecting left -> moving left")
                        car.moveCar(CAR_DIRECTION.LEFT)
                        sleep(0.1)
                        car.moveCar(CAR_DIRECTION.FWD)
                        sleep(0.1)

                    if 0 <= slope <= 7:
                        # go right
                        log.debug("detecting right -> moving right")
                        car.moveCar(CAR_DIRECTION.RIGHT)
                        sleep(0.1)
                        car.moveCar(CAR_DIRECTION.FWD)
                        sleep(0.1)

                    if slope > 7 or slope == 'inf':
                        # go forward
                        log.debug("Moving forward")

                        # Moving x2+100px due to the camera lens is not in the middle.
                        # if your web camera with is in the middle, please remove it.
                        x2 += 100

                        # keeping car on the middle (30 = gap of the middle line)
                        if x2 > (CAMERA_WIDTH / 2) + 30:
                            log.debug("not in the middle -> move right")
                            car.moveCar(CAR_DIRECTION.RIGHT)
                            sleep(0.1)

                        if x2 <= (CAMERA_WIDTH / 2) - 30:
                            log.debug("not in the middle -> move left")
                            car.moveCar(CAR_DIRECTION.LEFT)
                            sleep(0.1)

                        car.moveCar(CAR_DIRECTION.FWD)
                else:
                    # if reaching here, there are 2 options:
                    # 1- car stopped on the red light (traffic light)
                    # 2- car stopped because it reached the end of road -> do U-Turn
                    car.moveCar(CAR_DIRECTION.STOP)

                    # wait 30 frames to make sure that car reached end of road
                    stop_on_u_turn_count += 1

                    if stop_on_u_turn_count == 20 and detecting_traffic_light is False and car.status == CAR_DIRECTION.FWD:
                        log.debug("Detecting U-Turn")
                        car.moveCar(CAR_DIRECTION.FWD)
                        sleep(2.5)
                        car.moveCar(CAR_DIRECTION.RIGHT)
                        sleep(6)
                        car.moveCar(CAR_DIRECTION.REVERSE)
                        sleep(1)
                        car.moveCar(CAR_DIRECTION.RIGHT)
                        sleep(1)
                        stop_on_u_turn_count = 0

            if args.manual_driving:
                inp = str(input())  # Take input from the terminal

                if inp == 'w':
                    car.moveCar(CAR_DIRECTION.FWD)
                elif inp == 'a':
                    car.moveCar(CAR_DIRECTION.LEFT)
                elif inp == 'd':
                    car.moveCar(CAR_DIRECTION.RIGHT)
                elif inp == 's':
                    car.moveCar(CAR_DIRECTION.REVERSE)
                elif inp == 'x':
                    car.moveCar(CAR_DIRECTION.STOP)

            # count the frames
            frame_count += 1

            if args.show_fps:
                elapsedTime = time() - t1
                fps = "(Playback) {:.1f} FPS".format(1 / elapsedTime)
                cv2.putText(orig_frame, fps, (0, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 0), 1, cv2.LINE_AA)

            if args.show_frame:
                cv2.imshow("Driving Pi", orig_frame)

            if cv2.waitKey(1):
                break  # ESC to quit

        # Release everything on finish
        releaseAll()

    except KeyboardInterrupt:
        releaseAll()


if __name__ == '__main__':
    sys.exit(main() or 0)
