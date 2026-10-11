/* SPDX-License-Identifier: LGPL-2.1-or-later
 * Copyright (C) 2026 HsingYun
 * FT9362 development backend: preserve raw frames and bypass matching.
 */

#include "entry.h"

#include <cstdlib>
#include <cstring>

static Ft9362Status copy_frame(const Ft9362Frame* frame, Ft9362Template* output) {
  *output = {};
  if (frame == nullptr || frame->data == nullptr || frame->size != FT9362_RAW_FRAME_SIZE) {
    return FT9362_STATUS_INVALID_ARGUMENT;
  }

  uint8_t* data = static_cast<uint8_t*>(std::malloc(frame->size));
  if (data == nullptr) {
    return FT9362_STATUS_OUT_OF_MEMORY;
  }

  std::memcpy(data, frame->data, frame->size);
  output->format = FT9362_RAW_FORMAT;
  output->data = data;
  output->size = frame->size;
  return FT9362_STATUS_OK;
}

Ft9362Status ft9362_enroll(const Ft9362Frame* frame, Ft9362Template* output) {
  if (output == nullptr) {
    return FT9362_STATUS_INVALID_ARGUMENT;
  }
  return copy_frame(frame, output);
}

Ft9362Status ft9362_verify(const Ft9362Frame* frame, const Ft9362TemplateView* reference, Ft9362VerifyResult* output) {
  if (output == nullptr) {
    return FT9362_STATUS_INVALID_ARGUMENT;
  }
  *output = {};
  if (reference == nullptr) {
    return FT9362_STATUS_INVALID_ARGUMENT;
  }

  Ft9362Status status = copy_frame(frame, &output->scanned);
  if (status == FT9362_STATUS_OK) {
    output->matched = 1;
  }
  return status;
}

Ft9362Status ft9362_identify(const Ft9362Frame* frame, const Ft9362TemplateView* gallery, size_t gallery_size, Ft9362IdentifyResult* output) {
  if (output == nullptr) {
    return FT9362_STATUS_INVALID_ARGUMENT;
  }
  *output = {};
  output->matched_index = FT9362_NO_MATCH;
  if (gallery_size != 0 && gallery == nullptr) {
    return FT9362_STATUS_INVALID_ARGUMENT;
  }

  Ft9362Status status = copy_frame(frame, &output->scanned);
  if (status == FT9362_STATUS_OK && gallery_size != 0) {
    output->matched_index = 0;
  }
  return status;
}

void ft9362_template_free(Ft9362Template* value) {
  if (value != nullptr) {
    std::free(value->data);
    *value = {};
  }
}
