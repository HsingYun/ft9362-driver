/* SPDX-License-Identifier: LGPL-2.1-or-later
 * Copyright (C) 2026 HsingYun
 * Exercise the standalone C ABI against the C++ backend without GLib.
 */

#include <assert.h>
#include <string.h>

#include "entry.h"

static uint8_t pixels[FT9362_RAW_FRAME_SIZE];

static void assert_frame(const Ft9362Template* value) {
  assert(value->format != NULL);
  assert(strcmp(value->format, "FT9362-RAW-v1") == 0);
  assert(value->size == sizeof(pixels));
  assert(value->data != pixels);
  assert(memcmp(value->data, pixels, sizeof(pixels)) == 0);
}

static void test_enroll(void) {
  Ft9362Frame frame = {pixels, sizeof(pixels)};
  Ft9362Template output = {0};
  assert(ft9362_enroll(&frame, &output) == FT9362_STATUS_OK);
  assert_frame(&output);
  pixels[0] ^= 0xff;
  assert(output.data[0] != pixels[0]);
  pixels[0] ^= 0xff;
  ft9362_template_free(&output);
  assert(output.format == NULL && output.data == NULL && output.size == 0);
  ft9362_template_free(&output);
  ft9362_template_free(NULL);
}

static void test_verify(void) {
  Ft9362Frame frame = {pixels, sizeof(pixels)};
  Ft9362TemplateView reference = {0};
  Ft9362VerifyResult output = {0};
  assert(ft9362_verify(&frame, &reference, &output) == FT9362_STATUS_OK);
  assert(output.matched == 1);
  assert_frame(&output.scanned);
  ft9362_template_free(&output.scanned);

  reference.format = "unrelated-format";
  reference.data = (const uint8_t*)"different finger";
  reference.size = strlen((const char*)reference.data);
  assert(ft9362_verify(&frame, &reference, &output) == FT9362_STATUS_OK);
  assert(output.matched == 1);
  assert_frame(&output.scanned);
  ft9362_template_free(&output.scanned);
}

static void test_identify(void) {
  Ft9362Frame frame = {pixels, sizeof(pixels)};
  Ft9362TemplateView gallery[2] = {{0}, {0}};
  Ft9362IdentifyResult output = {0};
  assert(ft9362_identify(&frame, NULL, 0, &output) == FT9362_STATUS_OK);
  assert(output.matched_index == FT9362_NO_MATCH);
  assert_frame(&output.scanned);
  ft9362_template_free(&output.scanned);

  assert(ft9362_identify(&frame, gallery, 2, &output) == FT9362_STATUS_OK);
  assert(output.matched_index == 0);
  assert_frame(&output.scanned);
  ft9362_template_free(&output.scanned);
}

static void test_invalid_arguments(void) {
  Ft9362Frame frame = {pixels, sizeof(pixels)};
  Ft9362Template output = {0};
  Ft9362TemplateView reference = {0};
  Ft9362VerifyResult verified = {0};
  Ft9362IdentifyResult identified = {0};
  assert(ft9362_enroll(&frame, NULL) == FT9362_STATUS_INVALID_ARGUMENT);
  assert(ft9362_verify(&frame, &reference, NULL) == FT9362_STATUS_INVALID_ARGUMENT);
  assert(ft9362_identify(&frame, NULL, 0, NULL) == FT9362_STATUS_INVALID_ARGUMENT);
  assert(ft9362_verify(&frame, NULL, &verified) == FT9362_STATUS_INVALID_ARGUMENT);
  assert(ft9362_identify(&frame, NULL, 1, &identified) == FT9362_STATUS_INVALID_ARGUMENT);
  assert(ft9362_enroll(NULL, &output) == FT9362_STATUS_INVALID_ARGUMENT);

  const Ft9362Frame invalid[] = {{NULL, sizeof(pixels)}, {pixels, 0}, {pixels, sizeof(pixels) - 1}, {pixels, sizeof(pixels) + 1}};
  for (size_t i = 0; i < sizeof(invalid) / sizeof(invalid[0]); i++) {
    verified.matched = 1;
    identified.matched_index = 0;
    assert(ft9362_enroll(&invalid[i], &output) == FT9362_STATUS_INVALID_ARGUMENT);
    assert(output.data == NULL && output.format == NULL && output.size == 0);
    assert(ft9362_verify(&invalid[i], &reference, &verified) == FT9362_STATUS_INVALID_ARGUMENT);
    assert(verified.matched == 0 && verified.scanned.data == NULL);
    assert(ft9362_identify(&invalid[i], &reference, 1, &identified) == FT9362_STATUS_INVALID_ARGUMENT);
    assert(identified.matched_index == FT9362_NO_MATCH && identified.scanned.data == NULL);
  }
}

int main(void) {
  for (size_t i = 0; i < sizeof(pixels); i++) {
    pixels[i] = (uint8_t)(i * 37 + 11);
  }
  test_enroll();
  test_verify();
  test_identify();
  test_invalid_arguments();
  return 0;
}
