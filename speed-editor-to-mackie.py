#!/usr/bin/env python3

"""
Simple mapping from Speed editor to Mackie controller (MCU) via MIDI.
Copyright (C) 2022 Ondrej Sindelar
Copyright (C) 2021 Sylvain Munaut <tnt@246tNt.com>
SPDX-License-Identifier: Apache-2.0
"""
import enum
import threading
from threading import Thread
from typing import List

import mido

from bmd import SpeedEditorKey, SpeedEditorLed, SpeedEditorJogLed, SpeedEditorJogMode, SpeedEditorHandler, SpeedEditor


class KeyModifiers(enum.IntEnum):
    NUDGE = 1


class MackieHandler(SpeedEditorHandler):
    # virtual midi loop ports (loopMIDI)
    midi_in_device = 'mackieIn'
    midi_out_device = 'mackieOut'

    JOG = {
        SpeedEditorKey.SHTL: (SpeedEditorJogLed.SHTL, SpeedEditorJogMode.RELATIVE_2),
        SpeedEditorKey.JOG: (SpeedEditorJogLed.JOG, SpeedEditorJogMode.RELATIVE_2),
        SpeedEditorKey.SCRL: (SpeedEditorJogLed.SCRL, SpeedEditorJogMode.RELATIVE_2),
    }

    JOG_SPEED_DIV_FACTOR = {
        SpeedEditorKey.SHTL: 20,
        SpeedEditorKey.JOG: 15,
        SpeedEditorKey.SCRL: 1
    }

    ZOOM_REPEAT_TIME = 0.15

    MCU_JOG_CC = 0x3c
    MCU_STOP = 0x5D
    MCU_PLAY = 0x5E
    MCU_REC = 0x5F
    MCU_UP = 0x60
    MCU_DOWN = 0x61
    MCU_LEFT = 0x62
    MCU_RIGHT = 0x63
    MCU_ZOOM = 0x64
    MCU_SCRUB = 0x65
    MCU_USER_A = 0x66
    MCU_USER_B = 0x67
    MCU_F1 = 0x36
    MCU_F2 = 0x37
    MCU_F3 = 0x38
    MCU_F4 = 0x39
    MCU_F5 = 0x3a
    MCU_F6 = 0x3b
    MCU_F7 = 0x3c
    MCU_F8 = 0x3d
    MCU_F9 = 0x3e
    MCU_F10 = 0x3f
    MCU_F11 = 0x40
    MCU_F12 = 0x41
    MCU_F13 = 0x42
    MCU_F14 = 0x43
    MCU_F15 = 0x44
    MCU_F16 = 0x45

    ZOOM_KEYS = (SpeedEditorKey.IN, SpeedEditorKey.OUT, SpeedEditorKey.TRIM_IN, SpeedEditorKey.TRIM_OUT)

    MODIFIER_KEY_MAP = {
        SpeedEditorKey.ROLL: KeyModifiers.NUDGE
    }

    RAMP_THRESHOLD = 5
    RAMP_FACTOR = 5

    modifiers = set()

    def __init__(self, se):
        self.zoom_timer_on = False
        self.se = se
        self.keys = set()
        self.leds = 0
        self.se.set_leds(self.leds)
        self.play_state = False
        self.zoom_mode = False
        self.scrub_mode = False
        self.jog_unsent = 0
        self._set_jog_mode_for_key(SpeedEditorKey.JOG)
        device_name = self.find_device_in_list(self.midi_in_device, mido.get_output_names())
        self.midi_out = mido.open_output(device_name)
        device_name = self.find_device_in_list(self.midi_out_device, mido.get_input_names())
        self.midi_in = mido.open_input(device_name)
        print(f"Connected to {self.midi_out} and {self.midi_in}")
        thread = Thread(target=self.receive_thread)
        thread.start()

    def find_device_in_list(self, device, list):
        full_device = next((n for n in list if n.startswith(device)), None)
        if not full_device:
            raise RuntimeError(f"Device {device} not found in list {list}")
        return full_device

    def receive_thread(self):
        "Receive MCU midi events -> register current states"
        while True:
            msg = self.midi_in.receive()
            if msg.type == 'note_on':
                if msg.note == self.MCU_PLAY:
                    self.play_state = msg.velocity > 0
                    # LED indication of play/rec
                    led_set = SpeedEditorLed.CAM1 | SpeedEditorLed.CAM2 | SpeedEditorLed.CAM3 | SpeedEditorLed.CAM4 | SpeedEditorLed.CAM5 | SpeedEditorLed.CAM6 | SpeedEditorLed.CAM7 | SpeedEditorLed.CAM8 | SpeedEditorLed.CAM9
                    self.leds &= ~led_set
                    if self.play_state:
                        self.leds |= led_set
                    self.se.set_leds(self.leds)
                if msg.note == self.MCU_ZOOM:
                    self.zoom_mode = msg.velocity > 0
                if msg.note == self.MCU_SCRUB:
                    self.scrub_mode = msg.velocity > 0

    def _set_jog_mode_for_key(self, key: SpeedEditorKey):
        if key not in self.JOG:
            return
        self.jog_mode = key
        self.se.set_jog_leds(self.JOG[key][0])
        self.se.set_jog_mode(self.JOG[key][1])

    def jog(self, mode: SpeedEditorJogMode, value):
        # increments come in multiples of 360
        value //= 360
        self.jog_unsent += value

        speed_div_factor = self.JOG_SPEED_DIV_FACTOR[self.jog_mode]
        value_to_send = self.jog_unsent // speed_div_factor
        if value_to_send == 0:
            return
        # remaining sub-step wheel rotation - save for later
        self.jog_unsent -= value_to_send * speed_div_factor
        if self.jog_mode == SpeedEditorKey.SHTL:
            self.send_midi_nudge_msgs(value_to_send)
        else:
            self.send_midi_jog_cc(value_to_send)

    def key(self, keys: List[SpeedEditorKey]):
        kl = ', '.join([k.name for k in keys])
        if not kl:
            kl = 'None'
        print(f"Keys held: {kl:s}")

        keys_set = set(keys)
        released = self.keys - keys_set
        pressed = keys_set - self.keys
        self.keys = keys_set
        for k in released:
            self.key_released(k)

        for k in pressed:
            self.key_pressed(k)

    def key_released(self, k):
        self.modifier_released(k)

    def key_pressed(self, k):
        # Select jog mode
        self._set_jog_mode_for_key(k)

        self.modifier_pressed(k)

        # stop/play -> send play/stop
        if k == SpeedEditorKey.STOP_PLAY:
            if self.play_state:
                self.send_midi_note(self.MCU_STOP)
            else:
                self.send_midi_note(self.MCU_PLAY)
        # red button -> record
        if k == SpeedEditorKey.FULL_VIEW:
            self.send_midi_note(self.MCU_REC)
        if k == SpeedEditorKey.CUT:
            self.send_midi_note(self.MCU_F3)
        if k == SpeedEditorKey.SPLIT:
            self.send_midi_note(self.MCU_F4)
        if k == SpeedEditorKey.LIVE_OWR:
            self.send_midi_note(self.MCU_F5)
        if k == SpeedEditorKey.ESC:
            self.send_midi_note(self.MCU_F6)
        if k == SpeedEditorKey.TRANS:
            self.send_midi_note(self.MCU_F7)
        if k == SpeedEditorKey.RIPL_DEL:
            self.send_midi_note(self.MCU_F8)
        if k in self.ZOOM_KEYS:
            self.zoom_handle_keys()

    def send_midi_note(self, note):
        self.midi_out.send(mido.Message('note_on', note=note, velocity=127))

    def send_midi_jog_cc(self, shift: int):
        abs_val_full = abs(shift)
        while abs_val_full > 0:
            abs_val = abs_val_full
            if abs_val > 63:
                abs_val = 63
            sign = int(shift < 0) << 6
            val = abs_val | sign
            self.midi_out.send(mido.Message('control_change', control=self.MCU_JOG_CC, value=val))
            abs_val_full -= abs_val

    def send_midi_nudge_msgs(self, shift: int):
        abs_val = abs(shift)
        if abs_val > self.RAMP_THRESHOLD:
            abs_val = (abs_val - self.RAMP_THRESHOLD) * self.RAMP_FACTOR + self.RAMP_THRESHOLD
        if KeyModifiers.NUDGE in self.modifiers:
            msg = self.MCU_USER_A if shift < 0 else self.MCU_USER_B
        else:
            msg = self.MCU_F1 if shift < 0 else self.MCU_F2
        for i in range(0, abs_val):
            self.send_midi_note(msg)

    def set_zoom_mode(self):
        if not self.zoom_mode:
            self.send_midi_note(self.MCU_ZOOM)

    def zoom_repeat(self):
        self.zoom_timer_on = False
        self.zoom_handle_keys()

    def set_zoom_timer(self):
        if not self.zoom_timer_on:
            self.zoom_timer_on = True
            zoom_timer = threading.Timer(self.ZOOM_REPEAT_TIME, self.zoom_repeat)
            zoom_timer.start()

    def zoom_handle_keys(self):
        zoom_pressed = False
        if any(k in self.keys for k in self.ZOOM_KEYS):
            self.set_zoom_mode()
            zoom_pressed = True
        if SpeedEditorKey.IN in self.keys:
            self.send_midi_note(self.MCU_RIGHT)
        if SpeedEditorKey.OUT in self.keys:
            self.send_midi_note(self.MCU_LEFT)
        if SpeedEditorKey.TRIM_IN in self.keys:
            self.send_midi_note(self.MCU_DOWN)
        if SpeedEditorKey.TRIM_OUT in self.keys:
            self.send_midi_note(self.MCU_UP)
        if zoom_pressed:
            self.set_zoom_timer()

    def modifier_pressed(self, k):
        modifier = self.MODIFIER_KEY_MAP.get(k)
        if modifier:
            self.modifiers.add(modifier)

    def modifier_released(self, k):
        modifier = self.MODIFIER_KEY_MAP.get(k)
        if modifier:
            self.modifiers.remove(modifier)


if __name__ == '__main__':
    se = SpeedEditor()
    se.authenticate()
    se.set_handler(MackieHandler(se))

    while True:
        se.poll()
