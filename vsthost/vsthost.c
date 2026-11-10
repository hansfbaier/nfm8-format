/* Minimal VST2 host to load FM8.dll, get/set chunks and parameters.
 * Usage: vsthost.exe <dll> <command> [args]
 * Commands:
 *   chunk <out.bin>                        - dump current plugin state chunk
 *   setchunk <in.bin> <out.bin>            - load chunk, dump it back
 *   param <index> <value> <out.bin>        - set param, dump chunk
 *   params [maxIndex]                      - print numParams and names
 *   preset <index> <out.bin>               - select preset (if program-based), dump chunk
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

typedef intptr_t (*audioMasterCallback)(void *effect, int32_t opcode, int32_t index, intptr_t value, void *ptr, float opt);

typedef struct AEffect {
    int32_t magic;              /* 'VstP' */
    intptr_t (*dispatcher)(struct AEffect*, int32_t, int32_t, intptr_t, void*, float);
    void (*process)(struct AEffect*, float**, float**, int32_t);
    void (*setParameter)(struct AEffect*, int32_t, float);
    float (*getParameter)(struct AEffect*, int32_t);
    int32_t numPrograms;
    int32_t numParams;
    int32_t numInputs;
    int32_t numOutputs;
    int32_t flags;
    intptr_t resvd1;
    intptr_t resvd2;
    int32_t initialDelay;
    int32_t realQualities;
    int32_t offQualities;
    float ioRatio;
    void *object;
    void *user;
    int32_t uniqueID;
    int32_t version;
    void (*processReplacing)(struct AEffect*, float**, float**, int32_t);
    void (*processDoubleReplacing)(struct AEffect*, double**, double**, int32_t);
    char future[56];
} AEffect;

/* opcodes */
enum {
    effOpen = 0,
    effClose = 1,
    effSetProgram = 2,
    effGetProgram = 3,
    effSetProgramName = 4,
    effGetProgramName = 5,
    effGetParamLabel = 6,
    effGetParamDisplay = 7,
    effGetParamName = 8,
    effSetSampleRate = 10,
    effSetBlockSize = 11,
    effMainsChanged = 12,
    effEditGetRect = 13,
    effEditOpen = 14,
    effEditClose = 15,
    effGetChunk = 23,
    effSetChunk = 24,
    effGetVendorString = 47,
    effGetProductString = 48,
    effGetVendorVersion = 49,
    effCanDo = 51,
    effGetParameterProperties = 56,
    effGetEffectName = 45,
};

/* audioMaster opcodes we need to answer */
enum {
    audioMasterVersion = 1,
    audioMasterCurrentId = 2,
    audioMasterIdle = 3,
    audioMasterGetTime = 7,
    audioMasterGetSampleRate = 10,
    audioMasterGetBlockSize = 11,
    audioMasterGetCurrentProcessLevel = 23,
    audioMasterGetVendorString = 32,
    audioMasterGetProductString = 33,
    audioMasterGetVendorVersion = 34,
    audioMasterCanDo = 37,
    audioMasterAutomate = 0,
};

static intptr_t hostCallback(void *effect, int32_t opcode, int32_t index, intptr_t value, void *ptr, float opt)
{
    (void)effect; (void)index; (void)value; (void)opt;
    switch (opcode) {
        case audioMasterVersion:       return 2400;
        case audioMasterCurrentId:     return 0;
        case audioMasterGetTime:       return 0;
        case audioMasterGetSampleRate: return 44100;
        case audioMasterGetBlockSize:  return 512;
        case audioMasterGetCurrentProcessLevel: return 0;
        case audioMasterGetVendorString:
            if (ptr) strcpy((char*)ptr, "VstHost");
            return 1;
        case audioMasterGetProductString:
            if (ptr) strcpy((char*)ptr, "VstHost");
            return 1;
        case audioMasterGetVendorVersion: return 1000;
        case audioMasterCanDo:
            if (ptr) {
                const char *s = (const char*)ptr;
                if (!strcmp(s, "supplyIdle") || !strcmp(s, "sendVstEvents") ||
                    !strcmp(s, "sendVstMidiEvent") || !strcmp(s, "receiveVstEvents") ||
                    !strcmp(s, "receiveVstMidiEvent") || !strcmp(s, "sizeWindow") ||
                    !strcmp(s, "openFileSelector") || !strcmp(s, "closeFileSelector") ||
                    !strcmp(s, "reportConnectionChanges") || !strcmp(s, "acceptIOChanges"))
                    return 1;
            }
            return 0;
        default:
            return 0;
    }
}


/* FM8 latches parameter changes in its audio engine; a parameter only shows up
 * in the state chunk after the plugin has actually rendered while active.
 * Call this after effSetParameter. */
static void commitParams(AEffect *fx, int blocks)
{
    float in[2][512], out[2][512];
    float *ins[2] = { in[0], in[1] }, *outs[2] = { out[0], out[1] };
    memset(in, 0, sizeof(in));
    memset(out, 0, sizeof(out));
    fx->dispatcher(fx, effSetBlockSize, 0, 512, NULL, 0);
    for (int b = 0; b < blocks; b++)
        if (fx->processReplacing)
            fx->processReplacing(fx, ins, outs, 512);
}

static int saveFile(const char *path, const void *data, size_t len)
{
    FILE *f = fopen(path, "wb");
    if (!f) { fprintf(stderr, "cannot write %s\n", path); return -1; }
    fwrite(data, 1, len, f);
    fclose(f);
    printf("wrote %s (%lu bytes)\n", path, (unsigned long)len);
    return 0;
}

static int loadFile(const char *path, void **out, size_t *len)
{
    FILE *f = fopen(path, "rb");
    if (!f) { fprintf(stderr, "cannot open %s\n", path); return -1; }
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    void *buf = malloc(n);
    if (!buf) { fclose(f); return -1; }
    fread(buf, 1, n, f);
    fclose(f);
    *out = buf;
    *len = n;
    return 0;
}

int main(int argc, char **argv)
{
    setvbuf(stdout, NULL, _IONBF, 0);
    if (argc < 2) {
        fprintf(stderr, "usage: vsthost <dll> <command> [args]\n");
        return 1;
    }
    const char *dllPath = argv[1];
    HMODULE mod = LoadLibraryA(dllPath);
    if (!mod) { fprintf(stderr, "LoadLibrary failed: %lu\n", GetLastError()); return 1; }
    AEffect *(*VSTPluginMain)(audioMasterCallback) =
        (AEffect *(*)(audioMasterCallback))GetProcAddress(mod, "VSTPluginMain");
    if (!VSTPluginMain) VSTPluginMain =
        (AEffect *(*)(audioMasterCallback))GetProcAddress(mod, "main");
    if (!VSTPluginMain) { fprintf(stderr, "no VSTPluginMain export\n"); return 1; }

    AEffect *fx = VSTPluginMain(hostCallback);
    if (!fx) { fprintf(stderr, "VSTPluginMain returned NULL\n"); return 1; }
    printf("magic=%c%c%c%c numParams=%d numPrograms=%d inputs=%d outputs=%d version=%d\n",
           (char)(fx->magic>>24), (char)((fx->magic>>16)&0xff), (char)((fx->magic>>8)&0xff), (char)(fx->magic&0xff),
           fx->numParams, fx->numPrograms, fx->numInputs, fx->numOutputs, fx->version);

    char name[256] = {0}, vendor[256] = {0};
    fx->dispatcher(fx, effGetEffectName, 0, 0, name, 0);
    fx->dispatcher(fx, effGetVendorString, 0, 0, vendor, 0);
    printf("effect: '%s' vendor: '%s'\n", name, vendor);

    fx->dispatcher(fx, effOpen, 0, 0, NULL, 0);
    fx->dispatcher(fx, effSetSampleRate, 0, 0, NULL, 44100.0f);
    fx->dispatcher(fx, effSetBlockSize, 0, 512, NULL, 0);
    fx->dispatcher(fx, effMainsChanged, 0, 1, NULL, 0);

    const char *cmd = argv[2];

    if (!strcmp(cmd, "chunk") && argc >= 4) {
        void *buf = NULL; intptr_t len = 0;
        len = fx->dispatcher(fx, effGetChunk, 0, 0, &buf, 0);
        if (len <= 0) { fprintf(stderr, "effGetChunk failed: %ld\n", (long)len); return 1; }
        saveFile(argv[3], buf, len);
    } else if (!strcmp(cmd, "setchunk") && argc >= 5) {
        void *in; size_t n;
        if (loadFile(argv[3], &in, &n)) return 1;
        intptr_t r = fx->dispatcher(fx, effSetChunk, 0, (intptr_t)n, in, 0);
        printf("effSetChunk -> %ld\n", (long)r);
        void *buf = NULL; intptr_t len = 0;
        len = fx->dispatcher(fx, effGetChunk, 0, 0, &buf, 0);
        if (len > 0) saveFile(argv[4], buf, len);
    } else if (!strcmp(cmd, "param") && argc >= 6) {
        int idx = atoi(argv[3]);
        float v = (float)atof(argv[4]);
        fx->setParameter(fx, idx, v);
        commitParams(fx, 2);
        void *buf = NULL; intptr_t len = 0;
        len = fx->dispatcher(fx, effGetChunk, 0, 0, &buf, 0);
        if (len > 0) saveFile(argv[5], buf, len);
    } else if (!strcmp(cmd, "scan") && argc >= 5) {
        /* scan: dump chunk per parameter set to the given value.
         * writes <outdir>/param_<idx>.bin for each param, one baseline first */
        const char *outdir = argv[3];
        float v = (float)atof(argv[4]);
        int first = (argc >= 6) ? atoi(argv[5]) : 0;
        int last  = (argc >= 7) ? atoi(argv[6]) : fx->numParams - 1;
        char path[512];
        void *buf = NULL; intptr_t len = 0;
        len = fx->dispatcher(fx, effGetChunk, 0, 0, &buf, 0);
        if (len <= 0) { fprintf(stderr, "getchunk failed\n"); return 1; }
        snprintf(path, sizeof(path), "%s/baseline.bin", outdir);
        saveFile(path, buf, len);
        for (int i = first; i <= last; i++) {
            /* reset state first: reload baseline chunk */
            fx->dispatcher(fx, effSetChunk, 0, (intptr_t)len, buf, 0);
            fx->setParameter(fx, i, v);
            /* run audio to let the engine apply the parameter */
            commitParams(fx, 2);
            /* two consecutive dumps: GUID noise differs, param bytes are stable */
            void *b2 = NULL; intptr_t l2 = 0;
            l2 = fx->dispatcher(fx, effGetChunk, 0, 0, &b2, 0);
            if (l2 > 0) {
                snprintf(path, sizeof(path), "%s/param_%04d_a.bin", outdir, i);
                saveFile(path, b2, l2);
            }
            void *b3 = NULL; intptr_t l3 = 0;
            l3 = fx->dispatcher(fx, effGetChunk, 0, 0, &b3, 0);
            if (l3 > 0) {
                snprintf(path, sizeof(path), "%s/param_%04d_b.bin", outdir, i);
                saveFile(path, b3, l3);
            }
            if (i % 100 == 0) printf("scanned %d\n", i);
        }
    } else if (!strcmp(cmd, "sweep") && argc >= 6) {
        /* sweep <outdir> <paramIdx> <v0> <v1> <steps> [fixIdx fixVal] : dump chunk per value */
        const char *outdir = argv[3];
        int idx = atoi(argv[4]);
        float v0 = (float)atof(argv[5]);
        float v1 = (float)atof(argv[6]);
        int steps = atoi(argv[7]);
        int fixIdx = -1; float fixVal = 0;
        if (argc >= 9) { fixIdx = atoi(argv[8]); fixVal = (float)atof(argv[9]); }
        char path[512];
        for (int s = 0; s <= steps; s++) {
            float v = v0 + (v1 - v0) * s / steps;
            if (fixIdx >= 0) fx->setParameter(fx, fixIdx, fixVal);
            fx->setParameter(fx, idx, v);
            commitParams(fx, 2);
            void *b = NULL; intptr_t l = 0;
            l = fx->dispatcher(fx, effGetChunk, 0, 0, &b, 0);
            if (l > 0) {
                snprintf(path, sizeof(path), "%s/sweep_%04d_%03d.bin", outdir, s, (int)(v*1000+0.5));
                saveFile(path, b, l);
            }
        }
    } else if (!strcmp(cmd, "probe") && argc >= 5) {
        /* probe <outdir> <byteStart> <byteEnd> :
           flip each bit in the chunk range, re-read chunk, report which bytes changed */
        const char *outdir = argv[3];
        int b0 = (int)strtol(argv[4], 0, 0);
        int b1 = (int)strtol(argv[5], 0, 0);
        void *buf = NULL; intptr_t len = 0;
        len = fx->dispatcher(fx, effGetChunk, 0, 0, &buf, 0);
        if (len <= 0) return 1;
        unsigned char *base = malloc(len);
        memcpy(base, buf, len);
        char *chunk = malloc(len + 16);
        FILE *report = fopen("probe_report.txt", "w");
        for (int b = b0; b <= b1 && b < (int)len; b++) {
            for (int bit = 0; bit < 8; bit++) {
                memcpy(chunk, base, len);
                chunk[b] ^= (char)(1 << bit);
                intptr_t r = fx->dispatcher(fx, effSetChunk, 0, len, chunk, 0);
                if (r <= 0) { fprintf(report, "byte %04x bit %d: setchunk rejected (%ld)\n", b, bit, (long)r); continue; }
                void *b2 = NULL; intptr_t l2 = 0;
                l2 = fx->dispatcher(fx, effGetChunk, 0, 0, &b2, 0);
                if (l2 <= 0) { fprintf(report, "byte %04x bit %d: getchunk failed\n", b, bit); continue; }
                unsigned char *d2 = (unsigned char*)b2;
                char path[512];
                snprintf(path, sizeof(path), "%s/probe_%04x_%d.bin", outdir, b, bit);
                FILE *f = fopen(path, "wb");
                if (f) { fwrite(d2, 1, l2, f); fclose(f); }
                fprintf(report, "flip %04x.%d (setchunk=%ld, len=%ld):", b, bit, (long)r, (long)l2);
                int n = l2 < (intptr_t)len ? (int)l2 : (int)len;
                for (int i = 0; i < n; i++) {
                    if (d2[i] != base[i]) fprintf(report, " %04x", i);
                }
                fprintf(report, "\n");
            }
        }
        fclose(report);
        free(base); free(chunk);
    } else if (!strcmp(cmd, "dispsweep") && argc >= 6) {
        /* dispsweep <outdir> <paramIdx> <v0> <v1> <steps> : chunk + display per value */
        const char *outdir = argv[3];
        int idx = atoi(argv[4]);
        float v0 = (float)atof(argv[5]);
        float v1 = (float)atof(argv[6]);
        int steps = atoi(argv[7]);
        char path[512];
        snprintf(path, sizeof(path), "%s/disps.txt", outdir);
        FILE *rep = fopen(path, "w");
        for (int s = 0; s <= steps; s++) {
            float v = v0 + (v1 - v0) * s / steps;
            fx->setParameter(fx, idx, v);
            commitParams(fx, 2);
            char disp[128] = {0}, pname[128] = {0};
            fx->dispatcher(fx, effGetParamDisplay, idx, 0, disp, 0);
            fx->dispatcher(fx, effGetParamName, idx, 0, pname, 0);
            void *b = NULL; intptr_t l = 0;
            l = fx->dispatcher(fx, effGetChunk, 0, 0, &b, 0);
            if (l > 0) {
                snprintf(path, sizeof(path), "%s/d_%03d.bin", outdir, s);
                saveFile(path, b, l);
            }
            fprintf(rep, "%d\t%.6f\t%s\t%s\t%ld\n", s, v, pname, disp, (long)l);
        }
        fclose(rep);
        printf("wrote %s/disps.txt\n", outdir);
    } else if (!strcmp(cmd, "params")) {
        int max = fx->numParams;
        if (argc >= 4) max = atoi(argv[3]);
        if (max > fx->numParams) max = fx->numParams;
        char pname[256];
        for (int i = 0; i < max; i++) {
            memset(pname, 0, sizeof(pname));
            fx->dispatcher(fx, effGetParamName, i, 0, pname, 0);
            printf("%3d: %-30s = %.4f\n", i, pname, fx->getParameter(fx, i));
        }
    } else if (!strcmp(cmd, "preset") && argc >= 5) {
        int idx = atoi(argv[3]);
        fx->dispatcher(fx, effSetProgram, 0, idx, NULL, 0);
        char pname[256] = {0};
        fx->dispatcher(fx, effGetProgramName, idx, 0, pname, 0);
        printf("program %d: '%s'\n", idx, pname);
        void *buf = NULL; intptr_t len = 0;
        len = fx->dispatcher(fx, effGetChunk, 0, 0, &buf, 0);
        if (len > 0) saveFile(argv[4], buf, len);
    } else {
        fprintf(stderr, "unknown command\n");
    }

    fx->dispatcher(fx, effMainsChanged, 0, 0, NULL, 0);
    fx->dispatcher(fx, effClose, 0, 0, NULL, 0);
    return 0;
}
