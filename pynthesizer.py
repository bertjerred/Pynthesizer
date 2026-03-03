import threading
import time
import numpy as np
import sounddevice as sd
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox
import json
import random
import sys
import os
from pedalboard import Pedalboard, Reverb
import mido
import mido.backends.rtmidi # Explicitly import backend

import queue
# --- CONFIGURATION ---
SAMPLE_RATE = 44100
BLOCK_SIZE = 256 
MAX_POLYPHONY = 8  # CPU Protection limit

# --- 1. GENERATE THE FREQUENCY MAP ---
# Use MIDI note numbers as keys for direct mapping
NOTE_FREQS = {i: 440.0 * (2.0 ** ((i - 69) / 12.0)) for i in range(128)}

# --- 2. THE SYNTHESIZER ENGINE ---
class SynthEngine:
    def __init__(self, sample_rate=SAMPLE_RATE):
        self.active_notes = {}
        self.lock = threading.Lock() 
        self.sample_rate = sample_rate

        # Global Filter Parameters 
        self.filter_cutoff = 2000.0
        self.filter_resonance = 0.4   
        self.filter_mode = 'LP' # 'LP', 'HP', 'BP'
        self.lp_state = 0.0
        self.bp_state = 0.0
        
        # LFO Parameters
        self.lfo_speed = 0.0  
        self.lfo_depth = 0.0  
        self.lfo_phase = 0.0
        
        # Oscillator Parameters
        self.osc_saw_level = 1.0
        self.osc_square_level = 0.0
        self.osc_pulse_width = 0.5
        self.osc_pwm_depth = 0.0
        self.osc_triangle_level = 0.0
        self.osc_sub = 0.0
        self.osc_noise = 0.0  
        
        # Envelope Parameters
        self.env_attack = 0.9   
        self.env_release = 0.5  
        self.env_to_filter = 0.0  
        
        # Delay FX Parameters 
        self.delay_level = 0.0
        self.delay_buffer_len = int(SAMPLE_RATE * 2.0) 
        self.delay_buffer = np.zeros(self.delay_buffer_len, dtype=np.float32)
        self.delay_head = 0
        self.delay_samples = int(0.4 * SAMPLE_RATE) 

        # Reverb FX Parameters
        self.reverb_level = 0.0
        # Initialize Pedalboard with a massive Room Size (0.95) for "Canyon" feel
        self.pb = Pedalboard([Reverb(room_size=0.95, damping=0.2, width=1.0, dry_level=1.0)])

    def note_on(self, note_val, velocity=1.0):
        """Triggers a note, or re-triggers it if already active."""
        if note_val in NOTE_FREQS:
            with self.lock:
                if len(self.active_notes) >= MAX_POLYPHONY and note_val not in self.active_notes:
                    # Voice stealing: remove the oldest note
                    oldest_note = list(self.active_notes.keys())[0]
                    del self.active_notes[oldest_note]

                if note_val not in self.active_notes:
                    self.active_notes[note_val] = {
                        'freq': NOTE_FREQS[note_val],
                        'phase': 0.0,
                        'phase_sub': 0.0,
                        'env': 0.0,       
                        'pressed': True,
                        'velocity': velocity
                    }
                else:
                    # Re-trigger note with new velocity
                    self.active_notes[note_val]['pressed'] = True
                    self.active_notes[note_val]['velocity'] = velocity
                    self.active_notes[note_val]['env'] = 0.0 # Reset envelope on re-trigger

    def note_off(self, note_val):
        """Begins the release phase for a note."""
        with self.lock:
            if note_val in self.active_notes:
                self.active_notes[note_val]['pressed'] = False

    def _poly_blep(self, phase, dt):
        """
        Generates a polynomial bandlimited step function (PolyBLEP).
        This is used to create anti-aliased oscillators by smoothing the
        discontinuities in naive waveforms like saw and square waves.
        """
        blep = np.zeros_like(phase)
        mask_left = phase < dt
        t_left = phase[mask_left] / dt
        blep[mask_left] = 2.0 * t_left - t_left**2 - 1.0
        mask_right = phase > 1.0 - dt
        t_right = (phase[mask_right] - 1.0) / dt
        blep[mask_right] = 2.0 * t_right + t_right**2 + 1.0
        return blep

    def _apply_filter(self, input_array, cutoff_freq):
        """Applies the state-variable filter to an audio buffer."""
        f = 2.0 * np.sin(np.pi * cutoff_freq / self.sample_rate)
        f = min(f, 0.9) 
        q = 1.0 - self.filter_resonance
        
        output_array = np.zeros_like(input_array)
        for i in range(len(input_array)):
            # State-variable filter calculations
            hp = input_array[i] - self.lp_state - q * self.bp_state
            self.bp_state += f * hp
            self.lp_state += f * self.bp_state
            
            # Select output based on mode
            if self.filter_mode == 'LP':
                output_array[i] = self.lp_state
            elif self.filter_mode == 'BP':
                output_array[i] = self.bp_state
            elif self.filter_mode == 'HP':
                output_array[i] = hp

        # Safety check for unstable filter states
        if not np.isfinite(self.lp_state):
            self.lp_state = 0.0
            self.bp_state = 0.0
            output_array.fill(0.0)
            
        return output_array

    def audio_callback(self, outdata, frames, time_info, status):
        """Main audio processing loop called by the sounddevice stream."""
        mixed_audio = np.zeros(frames, dtype=np.float32)
        max_env = 0.0 

        # --- LFO CALCULATION (once per block) ---
        lfo_value = 0.0
        if self.lfo_speed > 0.1:
            # Use the LFO value from the current phase for this block
            lfo_value = np.sin(self.lfo_phase)
            
            # Then, advance the phase for the *next* block
            self.lfo_phase += 2 * np.pi * self.lfo_speed * (frames / self.sample_rate)
            self.lfo_phase %= (2 * np.pi)

        with self.lock:
            for note_val in list(self.active_notes.keys()):
                state = self.active_notes[note_val]
                
                if state['pressed']:
                    att_inc = 0.001 + (self.env_attack ** 4)
                    if state['env'] < 1.0:
                        state['env'] += att_inc
                        if state['env'] > 1.0: state['env'] = 1.0
                else:
                    rel_mult = 0.999 - (self.env_release * 0.4)
                    state['env'] *= rel_mult

                if state['env'] < 0.001 and not state['pressed']:
                    del self.active_notes[note_val]
                    continue
                
                if state['env'] > max_env:
                    max_env = state['env']

                # --- OSCILLATORS ---
                dt = state['freq'] / self.sample_rate
                phase_start = state['phase']
                phase_end = phase_start + state['freq'] * (frames / self.sample_rate)
                t_array = np.linspace(phase_start, phase_end, frames, endpoint=False)
                phase_norm = t_array % 1.0

                osc_mix = np.zeros(frames, dtype=np.float32)

                # Saw Wave
                if self.osc_saw_level > 0.01:
                    saw = 2.0 * phase_norm - 1.0
                    saw -= self._poly_blep(phase_norm, dt)
                    osc_mix += saw * self.osc_saw_level

                # Square Wave (Bandlimited)
                if self.osc_square_level > 0.01:
                    # Calculate current pulse width, modulated by LFO
                    current_pw = self.osc_pulse_width + lfo_value * self.osc_pwm_depth
                    current_pw = np.clip(current_pw, 0.05, 0.95) # Avoid extremes

                    sqr = np.full_like(phase_norm, 1.0)
                    sqr[phase_norm > current_pw] = -1.0
                    sqr += self._poly_blep(phase_norm, dt)
                    sqr -= self._poly_blep((phase_norm - current_pw + 1.0) % 1.0, dt)
                    osc_mix += sqr * self.osc_square_level
                
                # Triangle Wave (naive)
                if self.osc_triangle_level > 0.01:
                    tri = 4.0 * np.abs(phase_norm - 0.5) - 1.0
                    osc_mix += tri * self.osc_triangle_level

                # Normalize oscillator mix to prevent clipping before other stages
                total_osc_level = self.osc_saw_level + self.osc_square_level + self.osc_triangle_level
                if total_osc_level > 1.0:
                    osc_mix /= total_osc_level

                final_wave = osc_mix

                # --- SUB OSC ---
                if self.osc_sub > 0.01:
                    freq_sub = state['freq'] * 0.5 # One octave down
                    phase_sub_start = state['phase_sub']
                    phase_sub_end = phase_sub_start + freq_sub * (frames / SAMPLE_RATE)
                    t_array_sub = np.linspace(phase_sub_start, phase_sub_end, frames, endpoint=False)
                    phase_norm_sub = t_array_sub % 1.0
                    
                    sub_wave = np.where(phase_norm_sub < 0.5, 1.0, -1.0)
                    final_wave = final_wave + (sub_wave * self.osc_sub)
                    state['phase_sub'] = phase_sub_end % 1.0
                
                # --- WHITE NOISE ---
                if self.osc_noise > 0.01:
                    noise_wave = np.random.uniform(-1.0, 1.0, frames)
                    final_wave += (noise_wave * self.osc_noise)

                # Apply envelope and velocity
                final_wave *= state['env'] * state['velocity']
                mixed_audio += final_wave
                state['phase'] = phase_end % 1.0

            # Master Filter
            if len(self.active_notes) > 0:
                current_cutoff = self.filter_cutoff
                
                if self.env_to_filter > 0.01:
                    env_mod_amount = max_env * self.env_to_filter
                    current_cutoff *= (2.0 ** (env_mod_amount * 5.0)) 
                
                # Apply LFO modulation to the filter cutoff
                current_cutoff += lfo_value * self.lfo_depth

                current_cutoff = max(80.0, min(16000.0, current_cutoff))
                mixed_audio = self._apply_filter(mixed_audio, current_cutoff)

        # Apply Delay FX
        if self.delay_level > 0.0:
            idxs = np.arange(frames)
            read_idxs = (self.delay_head + idxs - self.delay_samples) % self.delay_buffer_len
            write_idxs = (self.delay_head + idxs) % self.delay_buffer_len
            
            read_idxs_prev = (read_idxs - 1) % self.delay_buffer_len
            delayed = self.delay_buffer[read_idxs]
            delayed_damp = (delayed + self.delay_buffer[read_idxs_prev]) * 0.5
            self.delay_buffer[write_idxs] = mixed_audio + (delayed_damp * 0.7)
            mixed_audio += delayed * self.delay_level
            self.delay_head = (self.delay_head + frames) % self.delay_buffer_len

        # --- Reverb FX ---
        if self.reverb_level > 0.0:
            # Update Wet Level based on knob (0.0 to 1.0)
            self.pb[0].wet_level = self.reverb_level * 0.6
            
            # Process audio (Pedalboard returns stereo usually, so we handle dimensions)
            processed = self.pb(mixed_audio, sample_rate=self.sample_rate, reset=False)
            
            # If Pedalboard returns stereo, mix it down to mono for sounddevice
            if processed.ndim > 1 and processed.shape[0] > 1:
                mixed_audio = np.mean(processed, axis=0)
            else:
                mixed_audio = processed

        # Final gain stage to prevent clipping on the output
        mixed_audio *= 0.15 
        outdata[:] = mixed_audio.reshape(-1, 1)

# --- 3. GUI CLASS ---
class SynthGui:
    def __init__(self, root, synth):
        self.root = root
        self.synth = synth
        self.midi_in_port = None
        self.midi_out_port = None
        self.midi_thread_running = False
        self.presets_dir = self._get_presets_path()
        self.midi_queue = queue.Queue()

        self.root.title("Pynthesizer")
        self.root.geometry("960x560")
        self.root.minsize(900, 520)
        
        self.root.configure(bg="#1a1a1a")
        style = ttk.Style()
        style.theme_use('clam') 
        
        bg_color = "#1a1a1a"
        frame_color = "#2b2b2b"
        text_color = "#00ffcc" 
        
        style.configure("TFrame", background=bg_color)
        style.configure("TLabelframe", background=frame_color, bordercolor="#333333")
        style.configure("TLabelframe.Label", background=frame_color, foreground=text_color, font=("Courier", 10, "bold"))
        style.configure("TLabel", background=frame_color, foreground="#eeeeee", font=("Courier", 9))
        style.configure("TScale", background=frame_color, troughcolor="#111111")
        style.configure("TButton", font=("Courier", 9, "bold"), background="#444444", foreground="#ffffff")
        
        # --- TOP BAR (MIDI, PRESETS & STATUS) ---
        header_row_1 = ttk.Frame(root, padding=(10, 10, 10, 0))
        header_row_1.pack(fill="x")

        header_row_2 = ttk.Frame(root, padding=(10, 0, 10, 10))
        header_row_2.pack(fill="x")

        # MIDI Port Selectors
        midi_frame = ttk.Frame(header_row_1)
        midi_frame.pack(side="left")
        ttk.Label(midi_frame, text="MIDI In:", font=("Courier", 9)).pack(side="left", padx=(0, 5))
        self.midi_in_var = tk.StringVar()
        self.midi_in_ports = ["None"] + mido.get_input_names()
        self.in_menu = ttk.Combobox(midi_frame, textvariable=self.midi_in_var, values=self.midi_in_ports, state="readonly", width=22)
        self.in_menu.pack(side="left", padx=5)
        self.in_menu.bind('<<ComboboxSelected>>', self.connect_midi)

        ttk.Label(midi_frame, text="MIDI Thru:", font=("Courier", 9)).pack(side="left", padx=(10, 5))
        self.midi_out_var = tk.StringVar()
        self.midi_out_ports = ["None"] + mido.get_output_names()
        self.out_menu = ttk.Combobox(midi_frame, textvariable=self.midi_out_var, values=self.midi_out_ports, state="readonly", width=22)
        self.out_menu.pack(side="left", padx=5)
        self.out_menu.bind('<<ComboboxSelected>>', self.connect_midi)

        # --- PRESET MANAGEMENT ---
        preset_frame = ttk.Frame(header_row_2)
        preset_frame.pack(side="left", pady=(5, 0))

        # Save section
        self.preset_save_name_var = tk.StringVar()
        preset_entry = ttk.Entry(preset_frame, textvariable=self.preset_save_name_var, width=15, font=("Courier", 9))
        preset_entry.pack(side="left", padx=(0, 5))
        preset_entry.insert(0, "New Preset")
        ttk.Button(preset_frame, text="SAVE", command=self.save_preset).pack(side="left")

        # Load section
        self.preset_load_var = tk.StringVar()
        self.preset_menu = ttk.Combobox(preset_frame, textvariable=self.preset_load_var, state="readonly", width=15)
        self.preset_menu.pack(side="left", padx=(10, 5))
        ttk.Button(preset_frame, text="LOAD", command=self.load_preset).pack(side="left")

        # --- MAIN UI LAYOUT ---
        bottom_frame = ttk.Frame(root, padding=(10, 5))
        bottom_frame.pack(fill="x", side="bottom")

        ttk.Button(bottom_frame, text="ABOUT", command=self.show_about).pack(side="left", padx=(0, 15))
        
        self.status_var = tk.StringVar(value="SYSTEM ONLINE: Select a MIDI Input")
        self.lbl_status = tk.Label(bottom_frame, textvariable=self.status_var, font=("Courier", 10, "bold"), bg=bg_color, fg="#ffaa00")
        self.lbl_status.pack(side="right", padx=10)

        main_frame = ttk.Frame(root)
        main_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        # 1. OSCILLATOR
        osc_frame = ttk.LabelFrame(main_frame, text=" OSCILLATOR ")
        osc_frame.pack(side="left", fill="both", padx=5, expand=True)
        
        ttk.Label(osc_frame, text="Saw Level").pack(anchor="w", padx=10, pady=(10,0))
        self.scale_saw = ttk.Scale(osc_frame, from_=0.0, to=1.0, orient="horizontal", command=lambda v: setattr(self.synth, 'osc_saw_level', float(v)))
        self.scale_saw.pack(fill="x", padx=10, pady=5)

        ttk.Label(osc_frame, text="Square Level").pack(anchor="w", padx=10)
        self.scale_square = ttk.Scale(osc_frame, from_=0.0, to=1.0, orient="horizontal", command=lambda v: setattr(self.synth, 'osc_square_level', float(v)))
        self.scale_square.pack(fill="x", padx=10, pady=5)

        ttk.Label(osc_frame, text="Pulse Width").pack(anchor="w", padx=10)
        self.scale_pw = ttk.Scale(osc_frame, from_=0.05, to=0.95, orient="horizontal", command=lambda v: setattr(self.synth, 'osc_pulse_width', float(v)))
        self.scale_pw.pack(fill="x", padx=10, pady=5)

        ttk.Label(osc_frame, text="Triangle Level").pack(anchor="w", padx=10)
        self.scale_triangle = ttk.Scale(osc_frame, from_=0.0, to=1.0, orient="horizontal", command=lambda v: setattr(self.synth, 'osc_triangle_level', float(v)))
        self.scale_triangle.pack(fill="x", padx=10, pady=5)

        ttk.Label(osc_frame, text="Sub Osc Level").pack(anchor="w", padx=10)
        self.scale_sub = ttk.Scale(osc_frame, from_=0.0, to=1.0, orient="horizontal", command=lambda v: setattr(self.synth, 'osc_sub', float(v)))
        self.scale_sub.pack(fill="x", padx=10, pady=5)

        ttk.Label(osc_frame, text="White Noise").pack(anchor="w", padx=10)
        self.scale_noise = ttk.Scale(osc_frame, from_=0.0, to=1.0, orient="horizontal", command=lambda v: setattr(self.synth, 'osc_noise', float(v)))
        self.scale_noise.pack(fill="x", padx=10, pady=5)

        # 2. FILTER (VCF)
        filter_frame = ttk.LabelFrame(main_frame, text=" FILTER (VCF) ")
        filter_frame.pack(side="left", fill="both", padx=5, expand=True)

        # --- Logarithmic Cutoff Control with Precise Entry ---
        self.log_cutoff_min = np.log(80.0)
        self.log_cutoff_max = np.log(16000.0)

        cutoff_frame = ttk.Frame(filter_frame, style="TFrame")
        cutoff_frame.pack(fill="x", padx=10, pady=(10,0))
        ttk.Label(cutoff_frame, text="Cutoff (80-16k Hz)").pack(side="left")
        
        self.cutoff_entry_var = tk.StringVar()
        style.configure("TEntry", fieldbackground="#2b2b2b", foreground="#eeeeee", insertcolor="#eeeeee", bordercolor="#333333")
        cutoff_entry = ttk.Entry(cutoff_frame, textvariable=self.cutoff_entry_var, width=7, justify='right', font=("Courier", 9))
        cutoff_entry.pack(side="right")
        cutoff_entry.bind("<Return>", self._update_cutoff_from_entry)
        cutoff_entry.bind("<FocusOut>", self._update_cutoff_from_entry)

        self.scale_cutoff = ttk.Scale(filter_frame, from_=0, to=100, orient="horizontal", command=self._update_cutoff_from_scale)
        self.scale_cutoff.pack(fill="x", padx=10, pady=5)
        
        ttk.Label(filter_frame, text="Resonance").pack(anchor="w", padx=10)
        self.scale_res = ttk.Scale(filter_frame, from_=0.1, to=0.9, orient="horizontal", command=lambda v: setattr(self.synth, 'filter_resonance', float(v)))
        self.scale_res.pack(fill="x", padx=10, pady=5)

        ttk.Label(filter_frame, text="Env -> Filter").pack(anchor="w", padx=10)
        self.scale_env_filt = ttk.Scale(filter_frame, from_=0.0, to=1.0, orient="horizontal", command=lambda v: setattr(self.synth, 'env_to_filter', float(v)))
        self.scale_env_filt.pack(fill="x", padx=10, pady=5)

        ttk.Label(filter_frame, text="LFO -> Filter").pack(anchor="w", padx=10)
        self.scale_lfo_dep = ttk.Scale(filter_frame, from_=0.0, to=4000.0, orient="horizontal", command=lambda v: setattr(self.synth, 'lfo_depth', float(v)))
        self.scale_lfo_dep.pack(fill="x", padx=10, pady=5)

        ttk.Label(filter_frame, text="Filter Mode").pack(anchor="w", padx=10, pady=(10,0))
        self.filter_mode_var = tk.StringVar(value=self.synth.filter_mode)
        filter_type_frame = ttk.Frame(filter_frame)
        filter_type_frame.pack(fill="x", padx=10, pady=5)
        style.configure("TRadiobutton", background=frame_color, foreground="#eeeeee", font=("Courier", 9))
        ttk.Radiobutton(filter_type_frame, text="LP", variable=self.filter_mode_var, value="LP", command=self.set_filter_mode).pack(side="left", expand=True)
        ttk.Radiobutton(filter_type_frame, text="HP", variable=self.filter_mode_var, value="HP", command=self.set_filter_mode).pack(side="left", expand=True)
        ttk.Radiobutton(filter_type_frame, text="BP", variable=self.filter_mode_var, value="BP", command=self.set_filter_mode).pack(side="left", expand=True)

        # 3. MODULATION
        mod_frame = ttk.LabelFrame(main_frame, text=" MODULATION ")
        mod_frame.pack(side="left", fill="both", padx=5, expand=True)

        # LFO Sub-section
        ttk.Label(mod_frame, text="LFO Speed").pack(anchor="w", padx=10, pady=(10,0))
        self.scale_lfo_spd = ttk.Scale(mod_frame, from_=0.0, to=20.0, orient="horizontal", command=lambda v: setattr(self.synth, 'lfo_speed', float(v)))
        self.scale_lfo_spd.pack(fill="x", padx=10, pady=5)

        ttk.Label(mod_frame, text="LFO -> PWM").pack(anchor="w", padx=10)
        self.scale_pwm_depth = ttk.Scale(mod_frame, from_=0.0, to=0.5, orient="horizontal", command=lambda v: setattr(self.synth, 'osc_pwm_depth', float(v)))
        self.scale_pwm_depth.pack(fill="x", padx=10, pady=5)

        # AMP ENVELOPE Sub-section
        ttk.Label(mod_frame, text="Attack Speed").pack(anchor="w", padx=10, pady=(10,0))
        self.scale_attack = ttk.Scale(mod_frame, from_=0.0, to=1.0, orient="horizontal", command=lambda v: setattr(self.synth, 'env_attack', float(v)))
        self.scale_attack.pack(fill="x", padx=10, pady=5)

        ttk.Label(mod_frame, text="Release Tail").pack(anchor="w", padx=10)
        self.scale_release = ttk.Scale(mod_frame, from_=0.0, to=1.0, orient="horizontal", command=lambda v: setattr(self.synth, 'env_release', float(v)))
        self.scale_release.pack(fill="x", padx=10, pady=5)

        # 4. FX
        fx_frame = ttk.LabelFrame(main_frame, text=" FX ")
        fx_frame.pack(side="left", fill="both", padx=5, expand=True)

        ttk.Label(fx_frame, text="Tape Delay").pack(anchor="w", padx=10, pady=(10,0))
        self.scale_delay = ttk.Scale(fx_frame, from_=0.0, to=1.0, orient="horizontal", command=lambda v: setattr(self.synth, 'delay_level', float(v)))
        self.scale_delay.pack(fill="x", padx=10, pady=5)

        ttk.Label(fx_frame, text="Canyon Reverb").pack(anchor="w", padx=10)
        self.scale_reverb = ttk.Scale(fx_frame, from_=0.0, to=1.0, orient="horizontal", command=lambda v: setattr(self.synth, 'reverb_level', float(v)))
        self.scale_reverb.pack(fill="x", padx=10, pady=5)

        # Set initial UI state from synth engine defaults
        self._refresh_preset_list() # Populate preset list on startup
        self._update_gui_from_synth()

        # Start MIDI polling in a background thread
        self.midi_thread_running = True
        self.midi_thread = threading.Thread(target=self._midi_worker, daemon=True)
        self.midi_thread.start()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.root.after(50, self._process_midi_queue)

    def _update_cutoff_from_scale(self, slider_val):
        """Converts linear slider position (0-100) to a logarithmic frequency."""
        pos = float(slider_val) / 100.0
        log_val = self.log_cutoff_min + (self.log_cutoff_max - self.log_cutoff_min) * pos
        freq_val = np.exp(log_val)
        self.synth.filter_cutoff = freq_val
        self.cutoff_entry_var.set(f"{freq_val:.0f}")

    def _update_cutoff_from_entry(self, event=None):
        """Updates synth and slider from the precise entry box."""
        try:
            freq_val = float(self.cutoff_entry_var.get())
            freq_val = np.clip(freq_val, 80.0, 16000.0)
            self.synth.filter_cutoff = freq_val
            self.cutoff_entry_var.set(f"{freq_val:.0f}") # Update entry with clipped value

            # Update slider position to match
            log_val = np.log(freq_val)
            pos = (log_val - self.log_cutoff_min) / (self.log_cutoff_max - self.log_cutoff_min)
            self.scale_cutoff.set(pos * 100.0)
        except (ValueError, tk.TclError):
            # On invalid input, reset entry to the current synth value
            current_freq = self.synth.filter_cutoff
            self.cutoff_entry_var.set(f"{current_freq:.0f}")

    def _set_cutoff_value(self, freq_val):
        """Helper to set the cutoff frequency from code (e.g., for presets, MIDI CC)."""
        freq_val = np.clip(freq_val, 80.0, 16000.0)
        self.cutoff_entry_var.set(f"{freq_val:.0f}")
        self._update_cutoff_from_entry()

    def _apply_state_to_synth(self, state):
        """Applies a dictionary state to the synth engine."""
        self.synth.osc_saw_level = state.get("osc_saw_level", 1.0)
        self.synth.osc_square_level = state.get("osc_square_level", 0.0)
        self.synth.osc_pulse_width = state.get("osc_pulse_width", 0.5)
        self.synth.osc_pwm_depth = state.get("osc_pwm_depth", 0.0)
        self.synth.osc_triangle_level = state.get("osc_triangle_level", 0.0)
        self.synth.osc_sub = state.get("osc_sub", 0.0)
        self.synth.osc_noise = state.get("osc_noise", 0.0)
        self.synth.filter_cutoff = state.get("filter_cutoff", 2000.0)
        self.synth.filter_resonance = state.get("filter_resonance", 0.4)
        self.synth.filter_mode = state.get("filter_mode", "LP")
        self.synth.env_attack = state.get("env_attack", 0.9)
        self.synth.env_release = state.get("env_release", 0.5)
        self.synth.env_to_filter = state.get("env_to_filter", 0.0)
        self.synth.lfo_speed = state.get("lfo_speed", 0.0)
        self.synth.lfo_depth = state.get("lfo_depth", 0.0)
        self.synth.delay_level = state.get("delay_level", 0.0)
        self.synth.reverb_level = state.get("reverb_level", 0.0)

    def _update_gui_from_synth(self):
        """Updates all GUI controls to match the current synth engine state."""
        self.scale_saw.set(self.synth.osc_saw_level)
        self.scale_square.set(self.synth.osc_square_level)
        self.scale_pw.set(self.synth.osc_pulse_width)
        self.scale_pwm_depth.set(self.synth.osc_pwm_depth)
        self.scale_triangle.set(self.synth.osc_triangle_level)
        self.scale_sub.set(self.synth.osc_sub)
        self.scale_noise.set(self.synth.osc_noise)
        self._set_cutoff_value(self.synth.filter_cutoff) # Update cutoff slider and entry
        self.scale_res.set(self.synth.filter_resonance)
        self.filter_mode_var.set(self.synth.filter_mode)
        self.set_filter_mode() # Update synth's filter mode from the radio button state
        self.scale_attack.set(self.synth.env_attack)
        self.scale_release.set(self.synth.env_release)
        self.scale_env_filt.set(self.synth.env_to_filter)
        self.scale_lfo_spd.set(self.synth.lfo_speed)
        self.scale_lfo_dep.set(self.synth.lfo_depth)
        self.scale_delay.set(self.synth.delay_level)
        self.scale_reverb.set(self.synth.reverb_level)

    def _get_presets_path(self):
        """Finds or creates the presets directory, robust for compiled executables."""
        if getattr(sys, 'frozen', False):
            # For a compiled executable (e.g., via PyInstaller)
            application_path = os.path.dirname(sys.executable)
        else:
            # For a standard Python script
            application_path = os.path.dirname(os.path.abspath(__file__))
        
        presets_path = os.path.join(application_path, 'presets')
        if not os.path.exists(presets_path):
            os.makedirs(presets_path, exist_ok=True)
        return presets_path

    def _refresh_preset_list(self):
        """Scans the presets directory and updates the load combobox."""
        try:
            presets = sorted([f.replace('.json', '') for f in os.listdir(self.presets_dir) if f.endswith('.json')])
            self.preset_menu['values'] = presets
            self.preset_load_var.set('') # Clear selection in the dropdown
        except Exception as e:
            self.status_var.set("ERROR: Can't read presets.")
            print(f"Error refreshing preset list: {e}")

    def _all_notes_off(self):
        """Sends a note-off message for all possible MIDI notes to prevent stuck notes."""
        for i in range(128):
            self.synth.note_off(i)

    def on_closing(self):
        """Handles window close event to shut down gracefully."""
        print("Closing MIDI ports and stopping worker thread...")
        self.midi_thread_running = False # Signal thread to stop
        if self.midi_in_port: self.midi_in_port.close()
        if self.midi_out_port: self.midi_out_port.close()
        self.root.destroy()

    def connect_midi(self, event=None):
        """Connects to the selected MIDI input and output ports."""
        in_port_name = self.midi_in_var.get()
        out_port_name = self.midi_out_var.get()

        if self.midi_in_port: self.midi_in_port.close()
        if self.midi_out_port: self.midi_out_port.close()

        try:
            if in_port_name and in_port_name != "None":
                self.midi_in_port = mido.open_input(in_port_name)
                self.status_var.set(f"MIDI IN: {os.path.basename(in_port_name)}")
            
            if out_port_name and out_port_name != "None":
                self.midi_out_port = mido.open_output(out_port_name)

        except Exception as e:
            self.status_var.set("MIDI Error")
            messagebox.showerror("MIDI Error", f"Could not open MIDI port: {e}")

    def _midi_worker(self):
        """Dedicated thread for polling MIDI input to avoid GUI blocking."""
        while self.midi_thread_running:
            if self.midi_in_port and not self.midi_in_port.closed:
                for msg in self.midi_in_port.iter_pending():
                    if self.midi_out_port and not self.midi_out_port.closed:
                        self.midi_out_port.send(msg)

                    if msg.type == 'note_on' and msg.velocity > 0:
                        self.synth.note_on(msg.note, msg.velocity / 127.0)
                    elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                        self.synth.note_off(msg.note)
                    elif msg.type == 'control_change':
                        # Map CC 74 (Cutoff) and 71 (Resonance)
                        if msg.control == 74:
                            normalized = msg.value / 127.0
                            cutoff_val = 80.0 * (200.0 ** normalized) # Exponential curve for MIDI CC
                            self.midi_queue.put(('cutoff', cutoff_val))
                        elif msg.control == 71:
                            normalized = msg.value / 127.0
                            new_res_val = 0.1 + (normalized * 0.8)
                            self.midi_queue.put(('resonance', new_res_val))
            
            time.sleep(0.001) # Small sleep to prevent the thread from using 100% CPU

    def _process_midi_queue(self):
        """Processes MIDI messages from the queue on the main GUI thread."""
        try:
            while True:
                message = self.midi_queue.get_nowait()
                control, value = message
                if control == 'cutoff':
                    self._set_cutoff_value(value)
                elif control == 'resonance':
                    self.scale_res.set(value)
        except queue.Empty:
            pass # The queue is empty, do nothing.
        self.root.after(50, self._process_midi_queue)

    def set_filter_mode(self):
        self.synth.filter_mode = self.filter_mode_var.get()

    def save_preset(self):
        preset_name = self.preset_save_name_var.get().strip()
        if not preset_name:
            self.status_var.set("ERROR: Preset name is empty.")
            messagebox.showwarning("Save Error", "Please enter a name for the preset.")
            return

        filepath = os.path.join(self.presets_dir, f"{preset_name}.json")
        try:
            state = {
                "osc_saw_level": self.synth.osc_saw_level,
                "osc_square_level": self.synth.osc_square_level,
                "osc_pulse_width": self.synth.osc_pulse_width,
                "osc_pwm_depth": self.synth.osc_pwm_depth,
                "osc_triangle_level": self.synth.osc_triangle_level,
                "osc_sub": self.synth.osc_sub,
                "osc_noise": self.synth.osc_noise,
                "filter_cutoff": self.synth.filter_cutoff,
                "filter_mode": self.synth.filter_mode,
                "filter_resonance": self.synth.filter_resonance,
                "env_attack": self.synth.env_attack,
                "env_release": self.synth.env_release,
                "env_to_filter": self.synth.env_to_filter,
                "lfo_speed": self.synth.lfo_speed,
                "lfo_depth": self.synth.lfo_depth,
                "delay_level": self.synth.delay_level,
                "reverb_level": self.synth.reverb_level
            }
            with open(filepath, 'w') as f:
                json.dump(state, f, indent=4)
            
            self.status_var.set(f"SAVED: {preset_name}")
            self._refresh_preset_list() # Refresh the preset dropdown
            self.preset_load_var.set(preset_name) # Select the new preset in the dropdown
        except Exception as e:
            self.status_var.set("ERROR: Could not save preset.")
            messagebox.showerror("Save Error", f"Failed to save preset file:\n{e}")

    def load_preset(self):
        preset_name = self.preset_load_var.get()
        if not preset_name:
            self.status_var.set("No preset selected to load.")
            return

        filepath = os.path.join(self.presets_dir, f"{preset_name}.json")
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r') as f:
                    state = json.load(f)

                # Apply the loaded state to the synth engine first
                self._apply_state_to_synth(state)
                # Then, update the entire GUI to reflect the new state
                self._update_gui_from_synth()

                self.preset_save_name_var.set(preset_name) # Update the save name field for convenience
                self.status_var.set(f"LOADED: {preset_name}")
            except Exception as e:
                self.status_var.set("ERROR: Could not load preset.")
                messagebox.showerror("Load Error", f"Failed to load preset file:\n{e}")

    def show_about(self):
        """Displays the about-program messagebox."""
        messagebox.showinfo(
            "About Pynthesizer",
            "Custom Polyphonic Synthesizer v1.0\nFeaturing bandlimited oscillators, state-variable filters, and dynamic FX.\n\n© 2026 Bert Jerred"
        )

# --- 4. THE MAIN EXECUTION ---
if __name__ == '__main__':
    synth = SynthEngine(sample_rate=SAMPLE_RATE)
    try:
        with sd.OutputStream(samplerate=SAMPLE_RATE, blocksize=BLOCK_SIZE, channels=1, callback=synth.audio_callback, dtype=np.float32):
            print("\n[+] Audio Stream Active. Launching GUI...")
            root = tk.Tk()
            app = SynthGui(root, synth)
            root.mainloop()
    except Exception as e:
        print(f"Audio Device Error: {e}")
