//!DESC hue_cycle — update per-pixel hue phase (temporal state)
//
// Pass 1: compare each pixel to its saved colour from last frame.
//   • If the pixel changed by more than PARAM_2 (the reset threshold),
//     the accumulated hue phase for that pixel resets to 0.
//   • Otherwise the phase advances by PARAM_1 (speed).
// Saves state to "hue_state" WITHOUT modifying MAIN.
//
// Pass 2: reads the updated state and applies the hue rotation.
//
// PARAM_1  speed      0 = very slow cycle (~60 s/revolution)
//                     1 = fast          (~1.7 s/revolution)
// PARAM_2  threshold  0 = sensitive — even noise restarts the cycle
//                     1 = coarse    — only large changes restart
// PARAM_3  saturation 0 = preserve original saturation
//                     1 = boost towards fully saturated (vivid)
// PARAM_4  intensity  0 = pass through original pixel unchanged
//                     1 = full hue-shifted output
//
//!HOOK MAIN
//!BIND HOOKED
//!BIND hue_state
//!SAVE hue_state
//!WIDTH HOOKED.w
//!HEIGHT HOOKED.h
//!COMPONENTS 4

#define PARAM_1 0.25   /* speed      0=slow  1=fast  */
#define PARAM_2 0.20   /* threshold  0=tight 1=loose */

vec4 hook() {
    // Current frame colour for this pixel
    vec4  curr       = textureLod(HOOKED_raw, HOOKED_pos, 0.0);

    // Previous state packed as (hue_phase, prev_r, prev_g, prev_b).
    // On the very first frame the texture is zero-initialised, which causes
    // all non-black pixels to exceed the threshold and start from phase 0.
    vec4  prev_state = textureLod(hue_state_raw, HOOKED_pos, 0.0);
    vec3  prev_color = prev_state.gba;
    float hue_phase  = prev_state.r;

    // Euclidean RGB distance between this frame and last saved colour
    float change = length(curr.rgb - prev_color);
    float thresh = mix(0.02, 0.40, PARAM_2);

    if (change > thresh) {
        // Pixel changed significantly — restart hue shift from zero
        hue_phase = 0.0;
    } else {
        // Pixel is stable — advance the hue cycle
        float speed = mix(0.0003, 0.0100, PARAM_1);
        hue_phase   = fract(hue_phase + speed);
    }

    // Pack new state: hue_phase in .r, current RGB in .gba for next frame
    return vec4(hue_phase, curr.r, curr.g, curr.b);
}


//!DESC hue_cycle — apply per-pixel hue shift
//!HOOK MAIN
//!BIND HOOKED
//!BIND hue_state

#define PARAM_3 0.50   /* saturation 0=keep  1=vivid */
#define PARAM_4 1.00   /* intensity  0=off   1=full  */

// ── HSV ↔ RGB (Inigo Quilez compact form) ────────────────────────────────────
vec3 rgb2hsv(vec3 c) {
    vec4 K = vec4(0.0, -1.0/3.0, 2.0/3.0, -1.0);
    vec4 p = mix(vec4(c.bg, K.wz), vec4(c.gb, K.xy), step(c.b, c.g));
    vec4 q = mix(vec4(p.xyw, c.r), vec4(c.r, p.yzx), step(p.x, c.r));
    float d = q.x - min(q.w, q.y);
    return vec3(abs(q.z + (q.w - q.y) / (6.0*d + 1e-10)),
                d / (q.x + 1e-10), q.x);
}

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

vec4 hook() {
    vec4  curr      = HOOKED_texOff(vec2(0.0));
    // hue_state was just written by Pass 1 this frame
    vec4  state     = textureLod(hue_state_raw, HOOKED_pos, 0.0);
    float hue_phase = state.r;

    vec3 hsv = rgb2hsv(curr.rgb);

    // Rotate hue by the accumulated per-pixel phase
    hsv.x = fract(hsv.x + hue_phase);

    // Optional saturation boost towards fully vivid colours
    hsv.y = clamp(hsv.y + PARAM_3 * (1.0 - hsv.y), 0.0, 1.0);

    vec3 shifted = hsv2rgb(hsv);

    // PARAM_4 lets the user dial back the effect while keeping state running
    return vec4(mix(curr.rgb, shifted, PARAM_4), curr.a);
}
