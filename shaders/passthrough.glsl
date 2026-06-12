//!DESC passthrough
//!HOOK MAIN
//!BIND HOOKED

#define PARAM_1 0.5
#define PARAM_2 0.5
#define PARAM_3 0.5
#define PARAM_4 0.5

vec4 hook() {
    return textureLod(HOOKED_raw, HOOKED_pos, 0.0);
}
