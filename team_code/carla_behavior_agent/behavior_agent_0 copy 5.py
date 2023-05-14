# Copyright (c) # Copyright (c) 2018-2020 CVC.
#
# This work is licensed under the terms of the MIT license.
# For a copy, see <https://opensource.org/licenses/MIT>.


""" This module implements an agent that roams around a track following random
waypoints and avoiding other vehicles. The agent also responds to traffic lights,
traffic signs, and has different possible configurations. """

import random
import numpy as np
import carla
import json
import datetime
import misc
import utils_sensors
from pynput import keyboard
import time
import math
from typing import List
from shapely.geometry import Polygon

# TODO: import the one with the correct index
# from basic_agent import BasicAgent
# from local_planner import RoadOption
BasicAgent = __import__("basic_agent_" + __file__.replace(".py", "").split("_")[-1]).BasicAgent
RoadOption = __import__("local_planner_" + __file__.replace(".py", "").split("_")[-1]).RoadOption
from route_manipulation import interpolate_trajectory

from behavior_types import Cautious, Aggressive, Normal

from misc import get_speed, positive, is_within_distance, compute_distance


DEBUG_SMALL = 'small'
DEBUG_MEDIUM = 'medium'
DEBUG_LARGE = 'large'

DEBUG_TYPE = {
    DEBUG_SMALL: [0.8, 0.1],
    DEBUG_MEDIUM: [0.5, 0.15],
    DEBUG_LARGE: [0.2, 0.2],
}  # Size, height

def draw_string(world, location, string='', color=(255, 255, 255, 255), life_time=-1):
    """Utility function to draw debugging strings"""
    v_shift, _ = DEBUG_TYPE.get(DEBUG_SMALL)
    l_shift = carla.Location(z=v_shift)
    color = carla.Color(*color)
    world.debug.draw_string(location + l_shift, string, False, color, life_time)

def draw_point(world, location, point_type=DEBUG_SMALL, color=(255, 255, 255, 255), life_time=-1):
    """Utility function to draw debugging points"""
    v_shift, size = DEBUG_TYPE.get(point_type, DEBUG_SMALL)
    l_shift = carla.Location(z=v_shift)
    color = carla.Color(*color)
    world.debug.draw_point(location + l_shift, size, color, life_time)

def draw_arrow(world, location1, location2, arrow_type=DEBUG_SMALL, color=(255, 255, 255, 255), life_time=-1):
    """Utility function to draw debugging points"""
    if location1 == location2:
        draw_point(world, location1, arrow_type, color, life_time)
    v_shift, thickness = DEBUG_TYPE.get(arrow_type, DEBUG_SMALL)
    l_shift = carla.Location(z=v_shift)
    color = carla.Color(*color)
    world.debug.draw_arrow(location1 + l_shift, location2 + l_shift, thickness, thickness, color, life_time)

class BehaviorAgent(BasicAgent):
    """
    BehaviorAgent implements an agent that navigates scenes to reach a given
    target destination, by computing the shortest possible path to it.
    This agent can correctly follow traffic signs, speed limitations,
    traffic lights, while also taking into account nearby vehicles. Lane changing
    decisions can be taken by analyzing the surrounding environment such as tailgating avoidance.
    Adding to these are possible behaviors, the agent can also keep safety distance
    from a car in front of it by tracking the instantaneous time to collision
    and keeping it in a certain range. Finally, different sets of behaviors
    are encoded in the agent, from cautious to a more aggressive ones.
    """

    def __init__(self, vehicle, behavior='normal', opt_dict={}, map_inst=None, grp_inst=None):
        """
        Constructor method.

            :param vehicle: actor to apply to local planner logic onto
            :param behavior: type of agent to apply
        """

        ################################################################
        self.index = int(__file__.replace(".py", "").split("_")[-1])
        ################################################################

        super().__init__(vehicle, opt_dict=opt_dict, map_inst=map_inst, grp_inst=grp_inst)
        self._look_ahead_steps = 0

        # Vehicle information
        self._speed = 0
        self._speed_limit = 0
        self._direction = None
        self._incoming_direction = None
        self._incoming_waypoint = None
        self._min_speed = 5
        self._behavior = None
        self._sampling_resolution = 4.5
        self.vehicle = vehicle

        self.vehicles = {}

        # Parameters for agent behavior
        if behavior == 'cautious':
            self._behavior = Cautious()

        elif behavior == 'normal':
            self._behavior = Normal()

        elif behavior == 'aggressive':
            self._behavior = Aggressive()

        # Load the color for the debug string on the vehicle
        with open("./config.json", "r") as f:
            d = json.load(f)
            self.color = d["colors"][self.index] if "colors" in d else (255, 0, 0, 255)

        self.finished_datetime: datetime.datetime = None
        self.finished_timeout = 30
        self.prev_offset = 0.0
        self.prev_speed = 0.0
        self.time_step = 0.05 # 20Hz
        self.step_distance = 0.5
        self.n_speed_prediction = 10

        ################################################################
        # Section for GA scoring system
        ################################################################
        with open(f"./GA_score/reached_end_{self.index}", "w") as fp:
            fp.write(str(False))
        ################################################################
        self.slow_down = False
        listener = keyboard.Listener(on_press=self.on_press)
        listener.start()

    def on_press(self, key):
        try:
            if key.char == "p":
                self.slow_down = not self.slow_down
                return
        except Exception as e:
            pass

    def _update_information(self):
        """
        This method updates the information regarding the ego
        vehicle based on the surrounding world.
        """
        self._speed = get_speed(self._vehicle)
        self._speed_limit = self._vehicle.get_speed_limit()
        self._local_planner.set_speed(self._speed_limit)
        self._direction = self._local_planner.target_road_option
        if self._direction is None:
            self._direction = RoadOption.LANEFOLLOW

        self._look_ahead_steps = int((self._speed_limit) / 10)

        self._incoming_waypoint, self._incoming_direction = self._local_planner.get_incoming_waypoint_and_direction(
            steps=self._look_ahead_steps)
        if self._incoming_direction is None:
            self._incoming_direction = RoadOption.LANEFOLLOW

    def print_state(self, state: str, line: int):
        draw_string(self._world, self._vehicle.get_location() - carla.Location(x=(line+1)*0.75), state, self.color)

    def get_vel_acc(self, vehicle):
        vel_x = sum([x.x for x in self.vehicles[vehicle.id][1]]) / self.n_speed_prediction
        vel_y = sum([x.y for x in self.vehicles[vehicle.id][1]]) / self.n_speed_prediction
        vel = carla.Vector3D(x=vel_x, y=vel_y, z=0)

        max_speed = self._speed_limit / 3.6
        if vel.length() > max_speed:
            vel_x *= max_speed / vel.length()
            vel_y *= max_speed / vel.length()
            vel = carla.Vector3D(x=vel_x, y=vel_y, z=0)
        return vel, 0.0

    def predict_locations(self, vehicle_list, time_step, max_distance=100, max_distance_wp=100):
        def dist(location): return location.distance(self._vehicle.get_location())

        location_list = []
        for vehicle in vehicle_list:
            loc = vehicle.get_location()
            if dist(loc) > max_distance:
                continue

            vel, acc = self.get_vel_acc(vehicle)
            
            vel_x = vel.x if abs(vel.x) > 1e-2 else 1e-2
            vel_y = vel.y if abs(vel.y) > 1e-2 else 1e-2

            x_prev = vel_x * (time_step - time_step*0.01) # + 0.5*acc.x * (time_step - time_step*0.01)**2
            x_new = vel_x * time_step # + 0.5*acc.x * time_step**2
            y_prev = vel_y * (time_step - time_step*0.01) # + 0.5*acc.y * (time_step - time_step*0.01)**2
            y_new = vel_y * time_step # + 0.5*acc.y * time_step**2

            distance = math.sqrt((x_new**2) + (y_new**2))

            wp = self._map.get_waypoint(loc)

            if distance < 0.3:
                loc_pred = loc
                rot_pred = wp.transform.rotation
            else:
                # offset_length = loc.distance(wp.transform.location)
                # wp_right = wp.transform.get_right_vector()
                wp_next = wp.next(distance)[0]
                loc_pred = wp_next.transform.location

                offset_x = loc.x - wp.transform.location.x # offset_length*wp_right.x
                offset_y = loc.y - wp.transform.location.y # offset_length*wp_right.y
                loc_pred = carla.Location(x=loc_pred.x + offset_x, 
                                        y=loc_pred.y + offset_y, 
                                        z=loc_pred.z)

                rot_pred = wp_next.transform.rotation       

            location_list.append((vehicle, loc_pred, rot_pred))
        return location_list

    def predict_ego_data(self, prev_offset, step):
        ego_wp, _ = self._local_planner.get_incoming_waypoint_and_direction(step)

        ego_transform_pred = ego_wp.transform
        ego_loc_pred = ego_transform_pred.location

        # Displace the wp to the side
        r_vec = ego_transform_pred.get_right_vector()
        offset_x = prev_offset*r_vec.x
        offset_y = prev_offset*r_vec.y

        ego_loc_pred = carla.Location(x=ego_loc_pred.x + offset_x, 
                                        y=ego_loc_pred.y + offset_y, 
                                        z=ego_loc_pred.z)
        return ego_wp, ego_transform_pred, ego_loc_pred

    def get_base_speed(self):
        return min([
            self._behavior.max_speed,
            self._speed_limit - self._behavior.speed_lim_dist])

    def get_lane(self, location):
        return self._map.get_waypoint(location, lane_type=carla.LaneType.Any).lane_id

    def update_vehicle(self, vehicle):
        loc = vehicle.get_location()
        if vehicle.id not in self.vehicles:
            self.vehicles[vehicle.id] = (loc, [carla.Vector3D(x=0.0, y=0.0, z=0.0), ])
        else:
            loc_prev, velocity_prevs = self.vehicles[vehicle.id]
            velocity = carla.Vector3D(x=(loc.x - loc_prev.x)/self.time_step,
                                      y=(loc.y - loc_prev.y)/self.time_step,
                                      z=0.0)

            if len(velocity_prevs) >= self.n_speed_prediction:
                velocity_prevs.pop(0)
            
            velocity_prevs.append(velocity)
            self.vehicles[vehicle.id] = (loc, velocity_prevs)

    def get_ego_time_from_step(self, step):
        return int(step * 0.25 * (self.get_base_speed() / 3.6))

    def has_lane(self, ego_wp, direction: str):
        # direction: ["right", "left"]
        if direction == "right":
            return ego_wp.get_right_lane() is not None
        return ego_wp.get_left_lane() is not None
    
    """
    def check_vehicles(self, ego_wp, locations, direction: str):
        # direction: ["left", "right", "self"]
        vehicles = []
        margin = 0.0

        if direction == "self":
            ego_wp_direction = ego_wp
        elif direction == "right":
            ego_wp_direction = ego_wp.get_right_lane()
        elif direction == "left":
            ego_wp_direction = ego_wp.get_left_lane()

        ego_bbox = self._vehicle.bounding_box

        ego_vertices = ego_bbox.get_world_vertices(ego_wp_direction.transform)
        for vehicle, location, rotation in locations:
            if misc.is_hero(vehicle):
                continue

            bbox = vehicle.bounding_box
            extent = carla.Vector3D(
                x=(bbox.extent.x if bbox.extent.x > 2.0 else 2.0) + margin,
                y=(bbox.extent.y if bbox.extent.y > 3.0 else 3.0) + margin,
                z=(bbox.extent.z if bbox.extent.z > 1.0 else 1.0) + margin
            )
            bbox = carla.BoundingBox(carla.Location(), extent)
            transform = carla.Transform(location, rotation)
            if any([bbox.contains(x, transform) for x in ego_vertices]):
                vehicles.append((vehicle, transform))
        return vehicles
    """

    def check_vehicles(self, ego_wp, locations, direction: str):
        # direction: ["left", "right", "self"]
        vehicles = []
        margin = 0.0

        if direction == "self":
            ego_wp_direction = ego_wp
        elif direction == "right":
            ego_wp_direction = ego_wp.get_right_lane()
        elif direction == "left":
            ego_wp_direction = ego_wp.get_left_lane()

        ego_bbox = self._vehicle.bounding_box

        ego_vertices = ego_bbox.get_world_vertices(ego_wp_direction.transform)
        ego_pol = Polygon([[v.x, v.y, v.z] for v in ego_vertices])
        for vehicle, location, rotation in locations:
            if misc.is_hero(vehicle):
                continue

            bbox = vehicle.bounding_box
            extent = carla.Vector3D(
                x=(bbox.extent.x if bbox.extent.x > 2.0 else 2.0) + margin,
                y=(bbox.extent.y if bbox.extent.y > 3.0 else 3.0) + margin,
                z=(bbox.extent.z if bbox.extent.z > 1.0 else 1.0) + margin
            )
            bbox = carla.BoundingBox(carla.Location(), extent)
            transform = carla.Transform(location, rotation)

            pol = Polygon([[v.x, v.y, v.z] for v in bbox.get_world_vertices(transform)])

            if ego_pol.intersects(pol):
                vehicles.append((vehicle, transform))
        return vehicles

    def check_occupied(self, ego_wp, locations, direction: str):
        return not self.check_free(ego_wp, locations, direction)

    def check_free(self, ego_wp, locations, direction: str):
        return len(self.check_vehicles(ego_wp, locations, direction)) == 0

    def run_step(self, debug=False):
        """
        Execute one step of navigation.

            :param debug: boolean for debugging
            :return control: carla.VehicleControl
        """
        if self.slow_down:
            time.sleep(1.5)

        self._update_information()

        #############################
        # Obstacle Management
        #############################
        # Starting info
        ego_vehicle_loc = self._vehicle.get_location()
        ego_vehicle_wp = self._map.get_waypoint(ego_vehicle_loc)
        ego_vehicle_transform = self._vehicle.get_transform()

        vehicle_list = self._world.get_actors().filter("*vehicle*")
        for vehicle in vehicle_list:
            self.update_vehicle(vehicle)
        #############################

        offsets = []
        _prev_offset = self.prev_offset
        stop_overtake = False
        speed_front = None
        right_free = True
        left_free = True
        steps_to_consider_offset = 14
        steps_to_consider_speed = 7
        base_max_steps = 14

        print("#######################################################")
        max_steps = base_max_steps
        step = -1
        while step < max_steps:


            #############################
            # Location prediction
            #############################
            if step == -1:
                ego_wp, ego_transform_pred, ego_loc_pred = ego_vehicle_wp, ego_vehicle_transform, ego_vehicle_loc
                _vehicle_list = []
                
                for vel in vehicle_list:
                    if misc.is_hero(vel):
                        continue
                    _distance, _angle = utils_sensors.compute_magnitude_angle_with_sign(vel.get_location(), ego_loc_pred, ego_transform_pred.rotation.yaw)
                    if -90 <= _angle <= 90 or ((-180 <= _angle < -90 or 90 < _angle <= 180) and _distance < 7):
                        _vehicle_list.append(vel)

                vehicle_list = _vehicle_list
                transform_list = [(x, x.get_location(), x.get_transform().rotation) for x in vehicle_list]

                # self._world.debug.draw_box(carla.BoundingBox(ego_transform_pred.location, self._vehicle.bounding_box.extent), 
                # ego_transform_pred.rotation, life_time=0.06)
            else:
                ego_wp, ego_transform_pred, ego_loc_pred = self.predict_ego_data(_prev_offset, self.get_ego_time_from_step(step))
                transform_list = self.predict_locations(vehicle_list, step * 0.25)

            if step < steps_to_consider_offset:
                for vel, location, _ in transform_list:
                    if misc.is_hero(vel):
                        continue
                    draw_point(self._world, location, color=(0, 128, 255, 255), life_time=self.time_step + 0.01)
                draw_point(self._world, ego_loc_pred, color=(255, 0, 0, 255), life_time=self.time_step + 0.01)
            # draw_point(self._world, ego_loc_pred, color=(255, 0, 0, 255) if not step == -1 else (255, 255, 0, 255), life_time=-1)
            #############################

            #############################
            # Lane proposal
            #############################
            # Normal lane
            if not _prev_offset < 0.0:
                # Front occupied and has Left lane and Left free => overtake
                if (self.check_occupied(ego_wp, transform_list, "self") and
                    self.has_lane(ego_wp, "left") and 
                    self.check_free(ego_wp, transform_list, "left")):
                    offset = -2.0
                    max_steps += 4
                elif (self.has_lane(ego_wp, "left") and 
                      self.check_occupied(ego_wp, transform_list, "left")):
                    offset = 0.75
                    _vel, transform = self.check_vehicles(ego_wp, transform_list, "left")[0]

                    self._world.debug.draw_box(carla.BoundingBox(ego_transform_pred.location, self._vehicle.bounding_box.extent), 
                                               ego_transform_pred.rotation, life_time=0.06)
                    self._world.debug.draw_box(carla.BoundingBox(transform.location, _vel.bounding_box.extent), 
                                               transform.rotation, life_time=0.06, color=carla.Color(0, 0, 255))
                    
                    self._world.debug.draw_box(carla.BoundingBox(ego_wp.get_left_lane().transform.location, _vel.bounding_box.extent), 
                                               ego_wp.get_left_lane().transform.rotation, life_time=0.06, color=carla.Color(0, 255, 0))
                    
                    print("left", step)
                # Else => follow road
                else:
                    offset = 0.0
            # Overtake lane
            else:
                # has Right and Right occupied => continue overtake
                if (self.check_occupied(ego_wp, transform_list, "self")):
                    offset = -2.0
                    max_steps += 4
                # Front occupied and has Right lane and Right free => stop overtake
                elif (self.check_occupied(ego_wp, transform_list, "self") and
                    self.has_lane(ego_wp, "left") and 
                    self.check_free(ego_wp, transform_list, "left")):
                    print("stop overtake", step)
                    stop_overtake = True
                    offset = 0.0
                # Else => follow road
                else:
                    offset = 0.0

            #############################
            # Right and Left free
            #############################
            if -1 <= step < 5 and self.check_occupied(ego_wp, transform_list, "self") and _prev_offset < 0.0:
                print("right not free", step)
                right_free = False

            if -1 <= step < 5 and self.has_lane(ego_wp, "left") and self.check_occupied(ego_wp, transform_list, "left") and not _prev_offset < 0.0:
                left_free = False
            #############################


            _prev_offset = offset
            if step < steps_to_consider_offset:
                offsets.append(offset)
            #############################


            #############################
            # Speed proposal
            #############################
            frontal_vehicles = self.check_vehicles(ego_wp, transform_list, "self")
            if step < steps_to_consider_speed and speed_front is None and frontal_vehicles and not _prev_offset < 0.0:
                speed_front = self.get_vel_acc(frontal_vehicles[0][0])[0].length()
            #############################

            step += 1

        #############################
        # Lane management
        #############################
        if any([x > 0.0 for x in offsets]) and right_free:
            print("Move right")
            offset_final = max(offsets)
        elif any([x < 0.0 for x in offsets]) and left_free:
            print("Move left")
            offset_final = min(offsets)
        else:
            print("Follow", left_free, right_free)
            offset_final = 0.0

        print("offset final:", offset_final)

        if offset_final < 0.0 and stop_overtake and right_free:
            print("\noveruled")
            offset_final = 0.0
        #############################


        #############################
        # Speed management
        #############################
        if offset_final < 0:
            speed_final = 45
        elif speed_front is not None:
            speed_final = speed_front * 3.6
            print("\nspeed front", speed_final)
        else:
            speed_final = self.get_base_speed()

        print("speed final:", speed_final)
        #############################

        self.prev_offset = offset_final

        self._local_planner.set_offset(offset_final)
        self._local_planner.set_speed(speed_final)

        control = self._local_planner.run_step(debug=debug)
        return control
        #############################