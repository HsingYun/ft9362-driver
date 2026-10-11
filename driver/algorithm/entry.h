/* SPDX-License-Identifier: LGPL-2.1-or-later
 * Copyright (C) 2026 HsingYun
 * Independent FT9362 processing interface, shared by C and C++ callers.
 */

#ifndef FOCALTECH_0752_ALGORITHM_ENTRY_H_
#define FOCALTECH_0752_ALGORITHM_ENTRY_H_

#include <stddef.h>
#include <stdint.h>

#define FT9362_RAW_FRAME_SIZE 12166
#define FT9362_RAW_FORMAT "FT9362-RAW-v1"
#define FT9362_NO_MATCH SIZE_MAX

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
  FT9362_STATUS_OK,
  FT9362_STATUS_INVALID_ARGUMENT,
  FT9362_STATUS_INVALID_TEMPLATE,
  FT9362_STATUS_OUT_OF_MEMORY,
  FT9362_STATUS_NOT_IMPLEMENTED,
  FT9362_STATUS_INTERNAL_ERROR
} Ft9362Status;

typedef struct {
  const uint8_t* data;
  size_t size;
} Ft9362Frame;

typedef struct {
  const char* format;
  const uint8_t* data;
  size_t size;
} Ft9362TemplateView;

typedef struct {
  const char* format;
  uint8_t* data;
  size_t size;
} Ft9362Template;

typedef struct {
  int matched;
  Ft9362Template scanned;
} Ft9362VerifyResult;

typedef struct {
  size_t matched_index;
  Ft9362Template scanned;
} Ft9362IdentifyResult;

Ft9362Status ft9362_enroll(const Ft9362Frame* frame, Ft9362Template* output);
Ft9362Status ft9362_verify(const Ft9362Frame* frame, const Ft9362TemplateView* reference, Ft9362VerifyResult* output);
Ft9362Status ft9362_identify(const Ft9362Frame* frame, const Ft9362TemplateView* gallery, size_t gallery_size, Ft9362IdentifyResult* output);
void ft9362_template_free(Ft9362Template* value);

#ifdef __cplusplus
}
#endif

#endif
