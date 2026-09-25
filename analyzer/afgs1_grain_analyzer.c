/*
 * AFGS1 grain analyzer
 *
 * Runs libaom's noise model - the one aomenc uses for --denoise-noise-level -
 * on every frame of a (source, encode) pair and writes the film grain
 * parameters each frame gets, as libaom's encoder derives them:
 *
 *   status = aom_noise_model_update(model, source, encode, flat_blocks)
 *   OK         -> use the running (combined) model
 *   DIFFERENT  -> restart the model from this frame (aom_noise_model_save_latest)
 *   otherwise  -> keep the previous estimate if there is one
 *   params     = aom_noise_model_get_grain_parameters(model)   (every frame)
 *
 * libaom's noise model is single-threaded. Its per-frame work (flat block
 * search, building and solving that frame's equations, measuring noise
 * strength) does not depend on other frames; only merging into the running
 * model does. So frames are decoded once, in order, their per-frame work is
 * spread over a thread pool, and the merge runs in frame order on the main
 * thread - giving exactly the results of the sequential code (checked with
 * --reference, which calls the unmodified aom_noise_model_update).
 *
 * The noise model source is libaom's own aom_dsp/noise_model.c, included
 * below unmodified (BSD 2-Clause, Alliance for Open Media).
 *
 * Input (stdin): raw planar frames, each the source stacked above the encode
 * (ffmpeg "vstack"), at --width x 2*--height.
 * Output: a filmgrn1 table whose E lines use frame numbers, one entry per run
 * of frames with identical parameters; progress lines "P <frames>" on stdout.
 */

#include "aom_dsp/noise_model.c"

#include <errno.h>
#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif
#include <pthread.h>
#include <stdint.h>

#define BLOCK_SIZE 32
#define LAG 3
#define DEFAULT_SEED 7391

/* ------------------------------------------------------------------ split update */

/* Per-frame half of aom_noise_model_update: everything that touches only the
 * model's parameters and its latest_state. Returns the status the full update
 * would return if it failed here, else AOM_NOISE_STATUS_OK; *failed_channel is
 * the channel whose per-frame part failed (the merge stops there too). */
static aom_noise_status_t observe_frame(aom_noise_model_t *const noise_model,
                                        const uint8_t *const data[3],
                                        const uint8_t *const denoised[3], int w, int h,
                                        int stride[3], int chroma_sub_log2[2],
                                        const uint8_t *const flat_blocks, int block_size,
                                        int *failed_channel) {
  const int num_blocks_w = (w + block_size - 1) / block_size;
  const int num_blocks_h = (h + block_size - 1) / block_size;
  int num_blocks = 0;
  *failed_channel = -1;
  for (int i = 0; i < 3; ++i) {
    equation_system_clear(&noise_model->latest_state[i].eqns);
    noise_model->latest_state[i].num_observations = 0;
    noise_strength_solver_clear(&noise_model->latest_state[i].strength_solver);
  }
  for (int i = 0; i < num_blocks_h * num_blocks_w; ++i) {
    if (flat_blocks[i]) num_blocks++;
  }
  if (num_blocks <= 1) return AOM_NOISE_STATUS_INSUFFICIENT_FLAT_BLOCKS;

  for (int channel = 0; channel < 3; ++channel) {
    int no_subsampling[2] = { 0, 0 };
    const uint8_t *alt_data = channel > 0 ? data[0] : 0;
    const uint8_t *alt_denoised = channel > 0 ? denoised[0] : 0;
    int *sub = channel > 0 ? chroma_sub_log2 : no_subsampling;
    const int is_chroma = channel != 0;
    if (!add_block_observations(noise_model, channel, data[channel], denoised[channel], w, h,
                                stride[channel], sub, alt_data, alt_denoised, stride[0],
                                flat_blocks, block_size, num_blocks_w, num_blocks_h)) {
      *failed_channel = channel;
      return AOM_NOISE_STATUS_INTERNAL_ERROR;
    }
    if (!ar_equation_system_solve(&noise_model->latest_state[channel], is_chroma)) {
      if (is_chroma) {
        set_chroma_coefficient_fallback_soln(&noise_model->latest_state[channel].eqns);
      } else {
        *failed_channel = channel;
        return AOM_NOISE_STATUS_INTERNAL_ERROR;
      }
    }
    add_noise_std_observations(noise_model, channel, noise_model->latest_state[channel].eqns.x,
                               data[channel], denoised[channel], w, h, stride[channel], sub,
                               alt_data, stride[0], flat_blocks, block_size, num_blocks_w,
                               num_blocks_h);
    if (!aom_noise_strength_solver_solve(&noise_model->latest_state[channel].strength_solver)) {
      *failed_channel = channel;
      return AOM_NOISE_STATUS_INTERNAL_ERROR;
    }
  }
  return AOM_NOISE_STATUS_OK;
}

static void copy_state(aom_noise_state_t *dst, const aom_noise_state_t *src) {
  equation_system_copy(&dst->eqns, &src->eqns);
  equation_system_copy(&dst->strength_solver.eqns, &src->strength_solver.eqns);
  dst->strength_solver.min_intensity = src->strength_solver.min_intensity;
  dst->strength_solver.max_intensity = src->strength_solver.max_intensity;
  dst->strength_solver.num_bins = src->strength_solver.num_bins;
  dst->strength_solver.num_equations = src->strength_solver.num_equations;
  dst->strength_solver.total = src->strength_solver.total;
  dst->num_observations = src->num_observations;
  dst->ar_gain = src->ar_gain;
}

/* Sequential half: the rest of aom_noise_model_update, in the same order,
 * using the latest_state computed for this frame by observe_frame. */
static aom_noise_status_t merge_frame(aom_noise_model_t *const noise_model,
                                      const aom_noise_model_t *const work,
                                      aom_noise_status_t observed, int failed_channel) {
  if (observed == AOM_NOISE_STATUS_INSUFFICIENT_FLAT_BLOCKS ||
      observed == AOM_NOISE_STATUS_INVALID_ARGUMENT)
    return observed;
  for (int c = 0; c < 3; ++c) copy_state(&noise_model->latest_state[c], &work->latest_state[c]);

  int y_model_different = 0;
  for (int channel = 0; channel < 3; ++channel) {
    const int is_chroma = channel != 0;
    if (channel == failed_channel) return AOM_NOISE_STATUS_INTERNAL_ERROR;
    if (channel == 0 && noise_model->combined_state[channel].strength_solver.num_equations > 0 &&
        is_noise_model_different(noise_model)) {
      y_model_different = 1;
    }
    if (y_model_different) continue;
    noise_model->combined_state[channel].num_observations +=
        noise_model->latest_state[channel].num_observations;
    equation_system_add(&noise_model->combined_state[channel].eqns,
                        &noise_model->latest_state[channel].eqns);
    if (!ar_equation_system_solve(&noise_model->combined_state[channel], is_chroma)) {
      if (is_chroma) {
        set_chroma_coefficient_fallback_soln(&noise_model->combined_state[channel].eqns);
      } else {
        return AOM_NOISE_STATUS_INTERNAL_ERROR;
      }
    }
    noise_strength_solver_add(&noise_model->combined_state[channel].strength_solver,
                              &noise_model->latest_state[channel].strength_solver);
    if (!aom_noise_strength_solver_solve(&noise_model->combined_state[channel].strength_solver)) {
      return AOM_NOISE_STATUS_INTERNAL_ERROR;
    }
  }
  return y_model_different ? AOM_NOISE_STATUS_DIFFERENT_NOISE_TYPE : AOM_NOISE_STATUS_OK;
}

/* ------------------------------------------------------------------ options */

typedef struct {
  int width, height, bit_depth, subx, suby, threads;
  long long start_frame;
  const char *output, *checkpoint, *resume;
  int checkpoint_every;
  int reference;
} Options;

static void usage(void) {
  fprintf(stderr,
          "usage: afgs1_grain_analyzer --width W --height H --bit-depth B --chroma 420|422|444\n"
          "         --output TABLE [--threads N] [--start-frame S]\n"
          "         [--checkpoint FILE [--checkpoint-every K]] [--resume FILE] [--reference]\n"
          "stdin: raw planar frames, source stacked above encode (W x 2H).\n");
  exit(2);
}

static void parse(int argc, char **argv, Options *o) {
  memset(o, 0, sizeof(*o));
  o->threads = 1;
  o->checkpoint_every = 1000;
  o->subx = o->suby = 1;
  for (int i = 1; i < argc; i++) {
    const char *a = argv[i];
    const char *v = i + 1 < argc ? argv[i + 1] : NULL;
#define NEXT()          \
  do {                  \
    if (!v) usage();    \
    i++;                \
  } while (0)
    if (!strcmp(a, "--width")) { NEXT(); o->width = atoi(v); }
    else if (!strcmp(a, "--height")) { NEXT(); o->height = atoi(v); }
    else if (!strcmp(a, "--bit-depth")) { NEXT(); o->bit_depth = atoi(v); }
    else if (!strcmp(a, "--chroma")) {
      NEXT();
      if (!strcmp(v, "420")) { o->subx = 1; o->suby = 1; }
      else if (!strcmp(v, "422")) { o->subx = 1; o->suby = 0; }
      else if (!strcmp(v, "444")) { o->subx = 0; o->suby = 0; }
      else usage();
    }
    else if (!strcmp(a, "--threads")) { NEXT(); o->threads = atoi(v); }
    else if (!strcmp(a, "--start-frame")) { NEXT(); o->start_frame = atoll(v); }
    else if (!strcmp(a, "--output")) { NEXT(); o->output = v; }
    else if (!strcmp(a, "--checkpoint")) { NEXT(); o->checkpoint = v; }
    else if (!strcmp(a, "--checkpoint-every")) { NEXT(); o->checkpoint_every = atoi(v); }
    else if (!strcmp(a, "--resume")) { NEXT(); o->resume = v; }
    else if (!strcmp(a, "--reference")) { o->reference = 1; }
    else usage();
#undef NEXT
  }
  if (o->width <= 0 || o->height <= 0 || o->bit_depth < 8 || o->bit_depth > 12 || !o->output)
    usage();
  if (o->threads < 1) o->threads = 1;
}

/* ------------------------------------------------------------------ frames */

typedef struct {
  uint8_t *buf;
  uint8_t *flat_blocks;
  aom_noise_model_t work;
  aom_noise_status_t status;
  int failed_channel;
  long long frame;
  int state; /* 0 empty, 1 filled, 2 observed */
} Slot;

typedef struct {
  const Options *o;
  size_t frame_bytes;
  int bps;
  int cw, ch;
  Slot *slots;
  int nslots;
  aom_flat_block_finder_t *finders;
  pthread_mutex_t lock;
  pthread_cond_t filled, observed, freed;
  long long next_read, next_observe;
  int eof, stop;
} Ctx;

static void plane_pointers(const Ctx *c, uint8_t *buf, const uint8_t *src[3], const uint8_t *enc[3]) {
  const Options *o = c->o;
  const size_t y_half = (size_t)o->width * o->height * c->bps;
  const size_t c_half = (size_t)c->cw * c->ch * c->bps;
  uint8_t *u = buf + 2 * y_half;
  uint8_t *v = u + 2 * c_half;
  src[0] = buf;       enc[0] = buf + y_half;
  src[1] = u;         enc[1] = u + c_half;
  src[2] = v;         enc[2] = v + c_half;
}

static int read_exact(uint8_t *dst, size_t n) {
  size_t got = 0;
  while (got < n) {
    size_t r = fread(dst + got, 1, n - got, stdin);
    if (r == 0) return 0;
    got += r;
  }
  return 1;
}

static void observe_slot(Ctx *c, Slot *s, const aom_flat_block_finder_t *finder) {
  const Options *o = c->o;
  const uint8_t *src[3], *enc[3];
  plane_pointers(c, s->buf, src, enc);
  int strides[3] = { o->width, c->cw, c->cw };
  int sub[2] = { o->subx, o->suby };
  aom_flat_block_finder_run(finder, src[0], o->width, o->height, strides[0], s->flat_blocks);
  s->status = observe_frame(&s->work, src, enc, o->width, o->height, strides, sub, s->flat_blocks,
                            BLOCK_SIZE, &s->failed_channel);
}

static void *reader_main(void *arg) {
  Ctx *c = (Ctx *)arg;
  for (;;) {
    pthread_mutex_lock(&c->lock);
    Slot *s = &c->slots[c->next_read % c->nslots];
    while (s->state != 0 && !c->stop) pthread_cond_wait(&c->freed, &c->lock);
    if (c->stop) { pthread_mutex_unlock(&c->lock); break; }
    pthread_mutex_unlock(&c->lock);
    int ok = read_exact(s->buf, c->frame_bytes);
    pthread_mutex_lock(&c->lock);
    if (!ok) {
      c->eof = 1;
      pthread_cond_broadcast(&c->filled);
      pthread_cond_broadcast(&c->observed);
      pthread_mutex_unlock(&c->lock);
      break;
    }
    s->frame = c->next_read++;
    s->state = 1;
    pthread_cond_broadcast(&c->filled);
    pthread_mutex_unlock(&c->lock);
  }
  return NULL;
}

typedef struct { Ctx *c; int id; } WorkerArg;

static void *worker_main(void *arg) {
  WorkerArg *wa = (WorkerArg *)arg;
  Ctx *c = wa->c;
  for (;;) {
    pthread_mutex_lock(&c->lock);
    Slot *s = NULL;
    for (;;) {
      if (c->stop) break;
      if (c->next_observe < c->next_read) {
        s = &c->slots[c->next_observe % c->nslots];
        c->next_observe++;
        break;
      }
      if (c->eof) break;
      pthread_cond_wait(&c->filled, &c->lock);
    }
    pthread_mutex_unlock(&c->lock);
    if (!s) break;
    observe_slot(c, s, &c->finders[wa->id]);
    pthread_mutex_lock(&c->lock);
    s->state = 2;
    pthread_cond_broadcast(&c->observed);
    pthread_mutex_unlock(&c->lock);
  }
  return NULL;
}

/* ------------------------------------------------------------------ output */

static int params_equal(const aom_film_grain_t *a, const aom_film_grain_t *b) {
  aom_film_grain_t x = *a, y = *b;
  x.random_seed = y.random_seed = 0;  /* seeds are applied per frame by the writer */
  return !memcmp(&x, &y, sizeof(x));
}

static void write_entry(FILE *f, long long start, long long end, const aom_film_grain_t *p) {
  fprintf(f, "E %lld %lld %d %d 1\n", start, end, p->apply_grain, p->random_seed);
  fprintf(f, "\tp %d %d %d %d %d %d %d %d %d %d %d %d\n", p->ar_coeff_lag, p->ar_coeff_shift,
          p->grain_scale_shift, p->scaling_shift, p->chroma_scaling_from_luma, p->overlap_flag,
          p->cb_mult, p->cb_luma_mult, p->cb_offset, p->cr_mult, p->cr_luma_mult, p->cr_offset);
  fprintf(f, "\tsY %d ", p->num_y_points);
  for (int i = 0; i < p->num_y_points; ++i)
    fprintf(f, " %d %d", p->scaling_points_y[i][0], p->scaling_points_y[i][1]);
  fprintf(f, "\n\tsCb %d", p->num_cb_points);
  for (int i = 0; i < p->num_cb_points; ++i)
    fprintf(f, " %d %d", p->scaling_points_cb[i][0], p->scaling_points_cb[i][1]);
  fprintf(f, "\n\tsCr %d", p->num_cr_points);
  for (int i = 0; i < p->num_cr_points; ++i)
    fprintf(f, " %d %d", p->scaling_points_cr[i][0], p->scaling_points_cr[i][1]);
  fprintf(f, "\n\tcY");
  const int n = 2 * p->ar_coeff_lag * (p->ar_coeff_lag + 1);
  for (int i = 0; i < n; ++i) fprintf(f, " %d", p->ar_coeffs_y[i]);
  fprintf(f, "\n\tcCb");
  for (int i = 0; i <= n; ++i) fprintf(f, " %d", p->ar_coeffs_cb[i]);
  fprintf(f, "\n\tcCr");
  for (int i = 0; i <= n; ++i) fprintf(f, " %d", p->ar_coeffs_cr[i]);
  fprintf(f, "\n");
}

/* ------------------------------------------------------------------ checkpoint */

#define CKPT_MAGIC 0x31534741u /* "AGS1" */
#define CKPT_VERSION 1u

static int write_eqns(FILE *f, const aom_equation_system_t *e) {
  const int n = e->n;
  return fwrite(&n, sizeof(n), 1, f) == 1 && fwrite(e->A, sizeof(double), (size_t)n * n, f) == (size_t)n * n &&
         fwrite(e->b, sizeof(double), n, f) == (size_t)n && fwrite(e->x, sizeof(double), n, f) == (size_t)n;
}

static int read_eqns(FILE *f, aom_equation_system_t *e) {
  int n;
  if (fread(&n, sizeof(n), 1, f) != 1 || n != e->n) return 0;
  return fread(e->A, sizeof(double), (size_t)n * n, f) == (size_t)n * n &&
         fread(e->b, sizeof(double), n, f) == (size_t)n && fread(e->x, sizeof(double), n, f) == (size_t)n;
}

typedef struct {
  long long next_frame;
  long long seg_start;
  int have_seg;
  aom_film_grain_t seg_params;
  aom_film_grain_t running; /* encoder's film_grain_params struct (keeps random_seed) */
  long long output_size;
} Progress;

static int save_checkpoint(const char *path, const aom_noise_model_t *m, const Progress *p) {
  char tmp[4096];
  snprintf(tmp, sizeof(tmp), "%s.tmp", path);
  FILE *f = fopen(tmp, "wb");
  if (!f) return 0;
  unsigned magic = CKPT_MAGIC, version = CKPT_VERSION;
  int ok = fwrite(&magic, 4, 1, f) == 1 && fwrite(&version, 4, 1, f) == 1 &&
           fwrite(p, sizeof(*p), 1, f) == 1;
  for (int c = 0; ok && c < 3; ++c) {
    const aom_noise_state_t *s = &m->combined_state[c];
    ok = write_eqns(f, &s->eqns) && write_eqns(f, &s->strength_solver.eqns) &&
         fwrite(&s->strength_solver.min_intensity, sizeof(double), 1, f) == 1 &&
         fwrite(&s->strength_solver.max_intensity, sizeof(double), 1, f) == 1 &&
         fwrite(&s->strength_solver.num_bins, sizeof(int), 1, f) == 1 &&
         fwrite(&s->strength_solver.num_equations, sizeof(int), 1, f) == 1 &&
         fwrite(&s->strength_solver.total, sizeof(double), 1, f) == 1 &&
         fwrite(&s->num_observations, sizeof(int), 1, f) == 1 &&
         fwrite(&s->ar_gain, sizeof(double), 1, f) == 1;
  }
  ok = fclose(f) == 0 && ok;
  if (!ok) { remove(tmp); return 0; }
  remove(path);
  return rename(tmp, path) == 0;
}

static int load_checkpoint(const char *path, aom_noise_model_t *m, Progress *p) {
  FILE *f = fopen(path, "rb");
  if (!f) return 0;
  unsigned magic = 0, version = 0;
  int ok = fread(&magic, 4, 1, f) == 1 && fread(&version, 4, 1, f) == 1 && magic == CKPT_MAGIC &&
           version == CKPT_VERSION && fread(p, sizeof(*p), 1, f) == 1;
  for (int c = 0; ok && c < 3; ++c) {
    aom_noise_state_t *s = &m->combined_state[c];
    ok = read_eqns(f, &s->eqns) && read_eqns(f, &s->strength_solver.eqns) &&
         fread(&s->strength_solver.min_intensity, sizeof(double), 1, f) == 1 &&
         fread(&s->strength_solver.max_intensity, sizeof(double), 1, f) == 1 &&
         fread(&s->strength_solver.num_bins, sizeof(int), 1, f) == 1 &&
         fread(&s->strength_solver.num_equations, sizeof(int), 1, f) == 1 &&
         fread(&s->strength_solver.total, sizeof(double), 1, f) == 1 &&
         fread(&s->num_observations, sizeof(int), 1, f) == 1 &&
         fread(&s->ar_gain, sizeof(double), 1, f) == 1;
  }
  fclose(f);
  return ok;
}

/* ------------------------------------------------------------------ main */

/* libaom encoder: aom_denoise_and_model_run() status handling + parameter derivation. */
static void derive_params(aom_noise_model_t *model, aom_noise_status_t status, aom_film_grain_t *fg) {
  int have_noise_estimate = 0;
  if (status == AOM_NOISE_STATUS_OK) {
    have_noise_estimate = 1;
  } else if (status == AOM_NOISE_STATUS_DIFFERENT_NOISE_TYPE) {
    aom_noise_model_save_latest(model);
    have_noise_estimate = 1;
  } else {
    have_noise_estimate = model->combined_state[0].strength_solver.num_equations > 0;
  }
  fg->apply_grain = 0;
  if (have_noise_estimate) {
    if (!aom_noise_model_get_grain_parameters(model, fg)) {
      fg->apply_grain = 0;
      return;
    }
    if (!fg->random_seed) fg->random_seed = DEFAULT_SEED;
  }
}

int main(int argc, char **argv) {
  Options o;
  parse(argc, argv, &o);
#ifdef _WIN32
  _setmode(_fileno(stdin), 0x8000 /* _O_BINARY */);
#endif
  const int use_highbd = o.bit_depth > 8;
  const aom_noise_model_params_t params = { AOM_NOISE_SHAPE_SQUARE, LAG, o.bit_depth, use_highbd };
  aom_noise_model_t model;
  if (!aom_noise_model_init(&model, params)) { fprintf(stderr, "noise model init failed\n"); return 1; }

  Ctx c;
  memset(&c, 0, sizeof(c));
  c.o = &o;
  c.bps = use_highbd ? 2 : 1;
  c.cw = (o.width + o.subx) >> o.subx;
  c.ch = (o.height + o.suby) >> o.suby;
  c.frame_bytes = 2 * ((size_t)o.width * o.height + 2 * (size_t)c.cw * c.ch) * c.bps;
  const int num_blocks = ((o.width + BLOCK_SIZE - 1) / BLOCK_SIZE) * ((o.height + BLOCK_SIZE - 1) / BLOCK_SIZE);
  const int workers = o.reference ? 0 : o.threads;
  c.nslots = o.reference ? 1 : o.threads + 4;
  c.slots = (Slot *)calloc(c.nslots, sizeof(Slot));
  c.finders = (aom_flat_block_finder_t *)calloc(workers > 0 ? workers : 1, sizeof(aom_flat_block_finder_t));
  for (int i = 0; i < c.nslots; ++i) {
    c.slots[i].buf = (uint8_t *)malloc(c.frame_bytes);
    c.slots[i].flat_blocks = (uint8_t *)malloc(num_blocks);
    if (!c.slots[i].buf || !c.slots[i].flat_blocks || !aom_noise_model_init(&c.slots[i].work, params)) {
      fprintf(stderr, "out of memory (%d frame buffers of %zu bytes)\n", c.nslots, c.frame_bytes);
      return 1;
    }
  }
  for (int i = 0; i < (workers > 0 ? workers : 1); ++i)
    aom_flat_block_finder_init(&c.finders[i], BLOCK_SIZE, o.bit_depth, use_highbd);

  Progress pr;
  memset(&pr, 0, sizeof(pr));
  pr.next_frame = o.start_frame;
  FILE *out = NULL;
  if (o.resume) {
    if (!load_checkpoint(o.resume, &model, &pr)) { fprintf(stderr, "cannot read checkpoint %s\n", o.resume); return 1; }
    if (pr.next_frame != o.start_frame) {
      fprintf(stderr, "checkpoint is at frame %lld, not %lld\n", pr.next_frame, o.start_frame);
      return 1;
    }
    out = fopen(o.output, "r+b");
    if (!out || fseek(out, (long)pr.output_size, SEEK_SET) != 0) { fprintf(stderr, "cannot reopen %s\n", o.output); return 1; }
    /* drop anything written after the checkpoint */
#ifdef _WIN32
    if (_chsize_s(_fileno(out), pr.output_size) != 0) { fprintf(stderr, "cannot truncate output\n"); return 1; }
#endif
  } else {
    out = fopen(o.output, "wb");
    if (!out) { fprintf(stderr, "cannot write %s: %s\n", o.output, strerror(errno)); return 1; }
    fprintf(out, "filmgrn1\n");
  }

  pthread_t reader, *threads = NULL;
  WorkerArg *wargs = NULL;
  c.next_read = c.next_observe = o.start_frame;
  if (!o.reference) {
    pthread_mutex_init(&c.lock, NULL);
    pthread_cond_init(&c.filled, NULL);
    pthread_cond_init(&c.observed, NULL);
    pthread_cond_init(&c.freed, NULL);
    pthread_create(&reader, NULL, reader_main, &c);
    threads = (pthread_t *)calloc(workers, sizeof(pthread_t));
    wargs = (WorkerArg *)calloc(workers, sizeof(WorkerArg));
    for (int i = 0; i < workers; ++i) {
      wargs[i].c = &c;
      wargs[i].id = i;
      pthread_create(&threads[i], NULL, worker_main, &wargs[i]);
    }
  }

  long long frame = o.start_frame;
  int exit_code = 0;
  for (;;) {
    aom_noise_status_t status;
    if (o.reference) {
      /* the unmodified library path, for checking the threaded one */
      Slot *s = &c.slots[0];
      if (!read_exact(s->buf, c.frame_bytes)) break;
      const uint8_t *src[3], *enc[3];
      plane_pointers(&c, s->buf, src, enc);
      int strides[3] = { o.width, c.cw, c.cw };
      int sub[2] = { o.subx, o.suby };
      aom_flat_block_finder_run(&c.finders[0], src[0], o.width, o.height, strides[0], s->flat_blocks);
      status = aom_noise_model_update(&model, src, enc, o.width, o.height, strides, sub,
                                      s->flat_blocks, BLOCK_SIZE);
    } else {
      Slot *s = &c.slots[frame % c.nslots];
      pthread_mutex_lock(&c.lock);
      while (!(s->state == 2 && s->frame == frame) && !(c.eof && frame >= c.next_read))
        pthread_cond_wait(&c.observed, &c.lock);
      if (c.eof && frame >= c.next_read) { pthread_mutex_unlock(&c.lock); break; }
      pthread_mutex_unlock(&c.lock);
      status = merge_frame(&model, &s->work, s->status, s->failed_channel);
      pthread_mutex_lock(&c.lock);
      s->state = 0;
      pthread_cond_broadcast(&c.freed);
      pthread_mutex_unlock(&c.lock);
    }

    derive_params(&model, status, &pr.running);
    if (!pr.have_seg) {
      pr.have_seg = 1;
      pr.seg_start = frame;
      pr.seg_params = pr.running;
    } else if (!params_equal(&pr.seg_params, &pr.running)) {
      write_entry(out, pr.seg_start, frame, &pr.seg_params);
      pr.seg_start = frame;
      pr.seg_params = pr.running;
    }
    /* libaom advances the seed after every shown frame */
    pr.running.random_seed += 3381;
    if (pr.running.random_seed == 0) pr.running.random_seed = DEFAULT_SEED;
    frame++;
    pr.next_frame = frame;

    if (o.checkpoint && (frame - o.start_frame) % o.checkpoint_every == 0) {
      fflush(out);
      pr.output_size = ftell(out);
      if (!save_checkpoint(o.checkpoint, &model, &pr)) fprintf(stderr, "warning: checkpoint not saved\n");
    }
    if (frame % 8 == 0) {
      printf("P %lld\n", frame);
      fflush(stdout);
    }
  }

  if (pr.have_seg) write_entry(out, pr.seg_start, frame, &pr.seg_params);
  if (fclose(out) != 0) { fprintf(stderr, "error writing %s\n", o.output); exit_code = 1; }
  if (!o.reference) {
    pthread_mutex_lock(&c.lock);
    c.stop = 1;
    pthread_cond_broadcast(&c.filled);
    pthread_cond_broadcast(&c.freed);
    pthread_mutex_unlock(&c.lock);
    pthread_join(reader, NULL);
    for (int i = 0; i < workers; ++i) pthread_join(threads[i], NULL);
  }
  printf("D %lld\n", frame);
  fflush(stdout);
  return exit_code;
}
