//!DESC bitcrush — mosaic block pixelation with area-averaged colour
//!HOOK MAIN
//!BIND HOOKED

#define PARAM_1 0.3    /* block size   */
#define PARAM_2 0.5    /* colour depth */
#define PARAM_3 0.0    /* gap width    */
#define PARAM_4 0.0    /* mix original */

// ── main ───────────────────────────────────────────────────────────────────
vec4 hook() {
    vec2 uv   = HOOKED_pos;
    vec2 size = HOOKED_size;

    // Block edge length in pixels: 1..256 (square-law — fine steps at low end)
    float blockPx = max(1.0, PARAM_1 * PARAM_1 * 255.0 + 1.0);

    // Optional grid gap — fixed-width black lines at block boundaries.
    // PARAM_3 maps 0..1 → 0..6 px, rounded to whole pixels so the line
    // width is the same regardless of block size.
    vec2  posInBlock = mod(uv * size, blockPx);
    float gap        = floor(PARAM_3 * 6.0 + 0.5);
    if (gap >= 0.5 && (posInBlock.x < gap || posInBlock.y < gap)) {
        return vec4(0.0, 0.0, 0.0, 1.0);
    }

    // Block origin (top-left) in normalised UV space
    vec2 blockOrigin = floor(uv * size / blockPx) * blockPx / size;
    // Step size for a 3×3 sample grid within the block
    vec2 bStep = vec2(blockPx) / (3.0 * size);

    // 3×3 area average — 9 evenly-spaced samples across the block.
    // Manual unroll avoids driver-dependent loop behaviour.
    vec3 avg = vec3(0.0);
    vec2 s;
    s = blockOrigin + bStep * vec2(0.5, 0.5); avg += textureLod(HOOKED_raw, s, 0.0).rgb;
    s = blockOrigin + bStep * vec2(1.5, 0.5); avg += textureLod(HOOKED_raw, s, 0.0).rgb;
    s = blockOrigin + bStep * vec2(2.5, 0.5); avg += textureLod(HOOKED_raw, s, 0.0).rgb;
    s = blockOrigin + bStep * vec2(0.5, 1.5); avg += textureLod(HOOKED_raw, s, 0.0).rgb;
    s = blockOrigin + bStep * vec2(1.5, 1.5); avg += textureLod(HOOKED_raw, s, 0.0).rgb;
    s = blockOrigin + bStep * vec2(2.5, 1.5); avg += textureLod(HOOKED_raw, s, 0.0).rgb;
    s = blockOrigin + bStep * vec2(0.5, 2.5); avg += textureLod(HOOKED_raw, s, 0.0).rgb;
    s = blockOrigin + bStep * vec2(1.5, 2.5); avg += textureLod(HOOKED_raw, s, 0.0).rgb;
    s = blockOrigin + bStep * vec2(2.5, 2.5); avg += textureLod(HOOKED_raw, s, 0.0).rgb;
    avg /= 9.0;

    // Colour quantisation — round each channel to `levels` discrete values.
    // PARAM_2=0 → 4 levels (very crushed); PARAM_2=1 → 64 levels (subtle).
    float levels = mix(4.0, 64.0, PARAM_2);
    avg = floor(avg * levels + 0.5) / levels;

    // Optional mix back to the sharp original pixel
    vec3 orig = textureLod(HOOKED_raw, uv, 0.0).rgb;
    return vec4(mix(avg, orig, PARAM_4), 1.0);
}
