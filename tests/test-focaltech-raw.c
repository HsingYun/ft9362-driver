/* SPDX-License-Identifier: LGPL-2.1-or-later
 * Copyright (C) 2026 HsingYun
 * Exercise the real driver callbacks and libfprint actions with simulated USB.
 */
#define FP_COMPONENT "focaltech0752"
#include "../libfprint/drivers/algorithm/entry.h"
#include "drivers_api.h"

static Ft9362Status fake_enroll(const Ft9362Frame* frame, Ft9362Template* output);
static Ft9362Status fake_verify(const Ft9362Frame* frame, const Ft9362TemplateView* reference, Ft9362VerifyResult* output);
static Ft9362Status fake_identify(const Ft9362Frame* frame, const Ft9362TemplateView* gallery, size_t gallery_size, Ft9362IdentifyResult* output);
static void fake_action_error(FpDevice* dev, GError* error);
static void fake_verify_complete(FpDevice* dev, GError* error);
static GUsbDevice* fake_get_usb_device(FpDevice* dev);
static gboolean fake_claim_interface(GUsbDevice* usb, gint interface, GUsbDeviceClaimInterfaceFlags flags, GError** error);
static gboolean fake_release_interface(GUsbDevice* usb, gint interface, GUsbDeviceClaimInterfaceFlags flags, GError** error);

static void fake_submit(FpiUsbTransfer* transfer, guint timeout, GCancellable* cancellable, FpiUsbTransferCallback callback, gpointer user_data);
#define fpi_usb_transfer_submit fake_submit
#define ft9362_enroll fake_enroll
#define ft9362_verify fake_verify
#define ft9362_identify fake_identify
#define fpi_device_action_error fake_action_error
#define fpi_device_verify_complete fake_verify_complete
#define fpi_device_get_usb_device fake_get_usb_device
#define g_usb_device_claim_interface fake_claim_interface
#define g_usb_device_release_interface fake_release_interface
#include "../libfprint/drivers/focaltech0752.c"
#undef fpi_usb_transfer_submit
#undef ft9362_enroll
#undef ft9362_verify
#undef ft9362_identify
#undef fpi_device_action_error
#undef fpi_device_verify_complete
#undef fpi_device_get_usb_device
#undef g_usb_device_claim_interface
#undef g_usb_device_release_interface

typedef enum {
  NORMAL,
  EMPTY_FRAME,
  OVERSIZED_FRAME,
  USB_ERROR,
  USB_STATUS_ERROR,
  USB_CAPTURE_ERROR,
  USB_PARTIAL_FRAME_ERROR,
  CLAIM_ERROR,
  RELEASE_ERROR,
  CANCEL_PENDING,
  CANCEL_TIMER,
  ZERO_POLL_WRITE,
  SHORT_POLL_WRITE,
  ZERO_CAPTURE_WRITE,
  SHORT_CAPTURE_WRITE,
  EMPTY_STATUS,
  SHORT_STATUS,
  OVERSIZED_STATUS,
  BAD_STATUS_HEADER,
  BAD_STATUS_TYPE,
  WAIT_THEN_READY,
  CUSTOM_TEMPLATE,
  VERIFY_NO_MATCH,
  IDENTIFY_SECOND,
  IDENTIFY_NO_MATCH,
  IDENTIFY_INVALID_INDEX,
  ALGORITHM_ERROR,
  OPTIONAL_SCAN,
  CANCEL_PARTIAL_FRAME,
  CANCEL_SUCCESSFUL_FRAME,
} Scenario;

static Scenario scenario;
static guint8 expected[FT9362_RAW_FRAME_SIZE];
static gsize frame_offset;
static guint captures;
static guint polls;
static guint status_reads;
static guint frame_reads;
static guint pending_transfers;
static guint action_completions;
static guint algorithm_calls;
static guint usb_failures;
static guint claims;
static guint releases;
static GUsbDevice* test_usb;
static const guint8 expected_poll_command[] = {0x02, 0x00, 0x03, 0x80, 0x02, 0x01, 0x80};
static const guint8 expected_capture_command[] = {0x02, 0x00, 0x01, 0x81, 0x80};

static Ft9362Status fake_enroll(const Ft9362Frame* frame, Ft9362Template* output) {
  algorithm_calls++;
  Ft9362Status status = ft9362_enroll(frame, output);
  if (status == FT9362_STATUS_OK && scenario == CUSTOM_TEMPLATE) {
    output->format = "FT9362-TEST-v1";
    output->size = 8;
  }
  return status;
}

static Ft9362Status fake_verify(const Ft9362Frame* frame, const Ft9362TemplateView* reference, Ft9362VerifyResult* output) {
  algorithm_calls++;
  Ft9362Status status = ft9362_verify(frame, reference, output);
  if (status == FT9362_STATUS_OK && scenario == VERIFY_NO_MATCH) {
    g_assert_cmpstr(reference->format, ==, "reference-v1");
    g_assert_cmpmem(reference->data, reference->size, expected, 8);
    output->matched = 0;
  } else if (status == FT9362_STATUS_OK && scenario == ALGORITHM_ERROR) {
    return FT9362_STATUS_INVALID_TEMPLATE;
  } else if (status == FT9362_STATUS_OK && scenario == OPTIONAL_SCAN) {
    ft9362_template_free(&output->scanned);
  }
  return status;
}

static Ft9362Status fake_identify(const Ft9362Frame* frame, const Ft9362TemplateView* gallery, size_t gallery_size, Ft9362IdentifyResult* output) {
  algorithm_calls++;
  Ft9362Status status = ft9362_identify(frame, gallery, gallery_size, output);
  if (status == FT9362_STATUS_OK && scenario == IDENTIFY_SECOND) {
    g_assert_cmpuint(gallery_size, ==, 2);
    g_assert_cmpstr(gallery[0].format, ==, "reference-v1");
    g_assert_cmpstr(gallery[1].format, ==, "reference-v1");
    g_assert_cmpmem(gallery[0].data, gallery[0].size, expected, 8);
    g_assert_cmpmem(gallery[1].data, gallery[1].size, expected + 8, 8);
    output->matched_index = 1;
  } else if (status == FT9362_STATUS_OK && scenario == IDENTIFY_NO_MATCH) {
    output->matched_index = FT9362_NO_MATCH;
  } else if (status == FT9362_STATUS_OK && scenario == IDENTIFY_INVALID_INDEX) {
    output->matched_index = gallery_size;
  }
  return status;
}

static void fake_action_error(FpDevice* dev, GError* error) {
  action_completions++;
  fpi_device_action_error(dev, error);
}

static void fake_verify_complete(FpDevice* dev, GError* error) {
  action_completions++;
  fpi_device_verify_complete(dev, error);
}

typedef struct {
  FpiUsbTransfer* transfer;
  GCancellable* cancellable;
  FpiUsbTransferCallback callback;
  gpointer user_data;
} Pending;

static gboolean cancel_cb(gpointer data) {
  g_cancellable_cancel(data);
  return G_SOURCE_REMOVE;
}

static gboolean deliver(gpointer data) {
  Pending* pending = data;
  FpiUsbTransfer* transfer = pending->transfer;
  GError* error = NULL;

  if (g_cancellable_set_error_if_cancelled(pending->cancellable, &error)) {
  } else if (scenario == USB_ERROR || (scenario == USB_STATUS_ERROR && transfer->endpoint == 0x81 && transfer->length == 64) || (scenario == USB_CAPTURE_ERROR && transfer->endpoint == 0x03 && transfer->length == sizeof(expected_capture_command)) ||
             (scenario == USB_PARTIAL_FRAME_ERROR && transfer->endpoint == 0x81 && transfer->length == 12166 && frame_offset == 4096)) {
    usb_failures++;
    error = g_error_new_literal(G_IO_ERROR, G_IO_ERROR_FAILED, "Simulated USB failure");
  } else if (transfer->endpoint == USB_ENDPOINT_OUT) {
    if (transfer->length == sizeof(expected_capture_command)) {
      g_assert_cmpmem(transfer->buffer, transfer->length, expected_capture_command, sizeof(expected_capture_command));
      frame_offset = 0;
      captures++;
    } else {
      g_assert_cmpmem(transfer->buffer, transfer->length, expected_poll_command, sizeof(expected_poll_command));
      polls++;
    }
    transfer->actual_length = transfer->length;
    /* Deliberately return no GError: the driver must validate OUT lengths,
     * including zero, independently of libfprint's short_is_error handling.
     */
    if ((scenario == ZERO_POLL_WRITE && transfer->length == sizeof(expected_poll_command)) || (scenario == ZERO_CAPTURE_WRITE && transfer->length == sizeof(expected_capture_command))) {
      transfer->actual_length = 0;
    } else if ((scenario == SHORT_POLL_WRITE && transfer->length == sizeof(expected_poll_command)) || (scenario == SHORT_CAPTURE_WRITE && transfer->length == sizeof(expected_capture_command))) {
      transfer->actual_length--;
    }
  } else if (transfer->length == STATUS_REPLY_CAPACITY) {
    const guint8 response[] = {0x02, 0x00, 0x02, 0x04, 0x01, 0x07, 0xf8};
    memcpy(transfer->buffer, response, sizeof(response));
    transfer->actual_length = sizeof(response);
    status_reads++;
    /* Only corrupt the first response, so a regression fails rather than
     * hanging forever if the driver incorrectly retries malformed packets.
     */
    if (status_reads == 1) {
      if (scenario == EMPTY_STATUS) {
        transfer->actual_length = 0;
      } else if (scenario == SHORT_STATUS) {
        transfer->actual_length = STATUS_REPLY_MIN_SIZE - 1;
      } else if (scenario == OVERSIZED_STATUS) {
        transfer->actual_length = transfer->length + 1;
      } else if (scenario == BAD_STATUS_HEADER) {
        transfer->buffer[0] = 0xff;
      } else if (scenario == BAD_STATUS_TYPE) {
        transfer->buffer[3] = 0xff;
      }
    }
    if (scenario == WAIT_THEN_READY && status_reads <= 2) {
      transfer->buffer[4] = 0;
    }
    if (scenario == CANCEL_TIMER) {
      transfer->buffer[4] = 0;
      /* Run after on_finger_status_received has installed its timer. */
      g_idle_add_full(G_PRIORITY_DEFAULT_IDLE, cancel_cb, g_object_ref(pending->cancellable), g_object_unref);
    }
  } else {
    frame_reads++;
    gsize size = MIN((gsize)4096, FT9362_RAW_FRAME_SIZE - frame_offset);
    if (scenario == EMPTY_FRAME) {
      size = 0;
    }
    if (scenario == OVERSIZED_FRAME && frame_offset > 0) {
      size = FT9362_RAW_FRAME_SIZE;
    }
    memset(transfer->buffer, 0, transfer->length);
    if (scenario != OVERSIZED_FRAME || frame_offset == 0) {
      memcpy(transfer->buffer, expected + frame_offset, size);
    }
    transfer->actual_length = size;
    frame_offset += size;
    if (scenario == CANCEL_SUCCESSFUL_FRAME && frame_offset == FT9362_RAW_FRAME_SIZE) {
      g_assert_no_error(error);
      g_assert_cmpuint(FPI_DEVICE_FOCALTECH0752(transfer->device)->capture.bytes_received, ==, FT9362_RAW_FRAME_SIZE - size);
      g_cancellable_cancel(pending->cancellable);
    }
  }

  g_assert_cmpuint(pending_transfers, >, 0);
  pending_transfers--;
  pending->callback(transfer, transfer->device, pending->user_data, error);
  fpi_usb_transfer_unref(transfer);
  g_object_unref(pending->cancellable);
  g_free(pending);
  return G_SOURCE_REMOVE;
}

static void fake_submit(FpiUsbTransfer* transfer, guint timeout, GCancellable* cancellable, FpiUsbTransferCallback callback, gpointer user_data) {
  Pending* pending = g_new0(Pending, 1);
  g_assert_cmpuint(pending_transfers, ==, 0);
  g_assert_nonnull(cancellable);
  if (transfer->endpoint == 0x03) {
    g_assert_cmpuint(timeout, ==, 1000);
    g_assert_true(transfer->short_is_error);
  } else {
    g_assert_cmpuint(transfer->endpoint, ==, 0x81);
    g_assert_true(transfer->length == 64 || transfer->length == 12166);
    g_assert_cmpuint(timeout, ==, transfer->length == 64 ? 1000 : 5000);
    g_assert_false(transfer->short_is_error);
  }
  pending->transfer = transfer;
  pending->cancellable = g_object_ref(cancellable);
  pending->callback = callback;
  pending->user_data = user_data;
  pending_transfers++;
  if (scenario == CANCEL_PENDING) {
    g_cancellable_cancel(cancellable);
  }
  if (scenario == CANCEL_PARTIAL_FRAME && transfer->endpoint == 0x81 && transfer->length == 12166 && frame_offset == 4096) {
    g_assert_cmpuint(FPI_DEVICE_FOCALTECH0752(transfer->device)->capture.bytes_received, ==, 4096);
    g_cancellable_cancel(cancellable);
  }
  g_idle_add(deliver, pending);
}

static GUsbDevice* fake_get_usb_device(FpDevice* dev) {
  g_assert_true(FPI_IS_DEVICE_FOCALTECH0752(dev));
  return test_usb;
}

static gboolean fake_claim_interface(GUsbDevice* usb, gint interface, GUsbDeviceClaimInterfaceFlags flags, GError** error) {
  g_assert_true(usb == test_usb);
  g_assert_cmpint(interface, ==, 0);
  g_assert_cmpint(flags, ==, 0);
  claims++;
  if (scenario == CLAIM_ERROR) {
    g_set_error_literal(error, G_USB_DEVICE_ERROR, G_USB_DEVICE_ERROR_BUSY, "Simulated claim failure");
    return FALSE;
  }
  return TRUE;
}

static gboolean fake_release_interface(GUsbDevice* usb, gint interface, GUsbDeviceClaimInterfaceFlags flags, GError** error) {
  g_assert_true(usb == test_usb);
  g_assert_cmpint(interface, ==, 0);
  g_assert_cmpint(flags, ==, 0);
  releases++;
  if (scenario == RELEASE_ERROR) {
    g_set_error_literal(error, G_USB_DEVICE_ERROR, G_USB_DEVICE_ERROR_FAILED, "Simulated release failure");
    return FALSE;
  }
  return TRUE;
}

static FpDevice* create_device(Scenario test_scenario) {
  FpDeviceClass* klass = g_type_class_ref(fpi_device_focaltech0752_get_type());
  FpDevice* dev;
  klass->type = FP_DEVICE_TYPE_VIRTUAL;
  scenario = test_scenario;
  captures = 0;
  polls = 0;
  status_reads = 0;
  frame_reads = 0;
  frame_offset = 0;
  g_assert_cmpuint(pending_transfers, ==, 0);
  action_completions = 0;
  algorithm_calls = 0;
  usb_failures = 0;
  claims = 0;
  releases = 0;
  for (gsize i = 0; i < FT9362_RAW_FRAME_SIZE; i++) {
    expected[i] = (i * 37 + 11) & 0xff;
  }
  dev = g_object_new(fpi_device_focaltech0752_get_type(), NULL);
  g_type_class_unref(klass);
  return dev;
}

static FpDevice* new_device(Scenario test_scenario) {
  FpDevice* dev = create_device(test_scenario);
  g_autoptr(GError) error = NULL;
  g_assert_true(fp_device_open_sync(dev, NULL, &error));
  g_assert_no_error(error);
  g_assert_true(fp_device_is_open(dev));
  g_assert_cmpuint(claims, ==, 1);
  return dev;
}

static void assert_raw(FpPrint* print) {
  g_autoptr(GVariant) data = NULL;
  g_autoptr(GVariant) bytes = NULL;
  const gchar* tag;
  gsize length;
  const guint8* frame;
  g_object_get(print, "fpi-data", &data, NULL);
  g_assert_true(g_variant_is_of_type(data, G_VARIANT_TYPE("(say)")));
  g_variant_get(data, "(&s@ay)", &tag, &bytes);
  g_assert_cmpstr(tag, ==, "FT9362-RAW-v1");
  frame = g_variant_get_fixed_array(bytes, &length, 1);
  g_assert_cmpmem(frame, length, expected, sizeof(expected));
}

static void assert_capture_idle(FpDevice* dev) {
  g_assert_cmpuint(pending_transfers, ==, 0);
  g_assert_false(FPI_DEVICE_FOCALTECH0752(dev)->capture.transfer_pending);
  g_assert_null(FPI_DEVICE_FOCALTECH0752(dev)->capture.machine);
  g_assert_cmpuint(FPI_DEVICE_FOCALTECH0752(dev)->capture.bytes_received, ==, 0);
  const guint8 cleared_frame[FT9362_RAW_FRAME_SIZE] = {0};
  g_assert_cmpmem(FPI_DEVICE_FOCALTECH0752(dev)->capture.frame, FT9362_RAW_FRAME_SIZE, cleared_frame, sizeof(cleared_frame));
}

static void close_device(FpDevice* dev) {
  GError* error = NULL;
  assert_capture_idle(dev);
  g_assert_true(fp_device_close_sync(dev, NULL, &error));
  g_assert_no_error(error);
  g_assert_false(fp_device_is_open(dev));
  g_object_unref(dev);
}

static void test_interface_error(gconstpointer data) {
  Scenario test_scenario = GPOINTER_TO_INT(data);
  FpDevice* dev = test_scenario == CLAIM_ERROR ? create_device(test_scenario) : new_device(test_scenario);
  g_autoptr(GError) error = NULL;
  if (test_scenario == CLAIM_ERROR) {
    g_assert_false(fp_device_open_sync(dev, NULL, &error));
    g_assert_error(error, G_USB_DEVICE_ERROR, G_USB_DEVICE_ERROR_BUSY);
    g_assert_cmpstr(error->message, ==, "Simulated claim failure");
    g_assert_cmpuint(releases, ==, 0);
  } else {
    g_assert_false(fp_device_close_sync(dev, NULL, &error));
    g_assert_error(error, G_USB_DEVICE_ERROR, G_USB_DEVICE_ERROR_FAILED);
    g_assert_cmpstr(error->message, ==, "Simulated release failure");
    g_assert_cmpuint(releases, ==, 1);
  }
  g_assert_cmpuint(claims, ==, 1);
  g_assert_false(fp_device_is_open(dev));
  assert_capture_idle(dev);

  scenario = NORMAL;
  g_clear_error(&error);
  g_assert_true(fp_device_open_sync(dev, NULL, &error));
  g_assert_no_error(error);
  g_assert_true(fp_device_is_open(dev));
  g_assert_cmpuint(claims, ==, 2);
  close_device(dev);
  g_assert_cmpuint(releases, ==, test_scenario == CLAIM_ERROR ? 1 : 2);
}

static void test_enroll(void) {
  FpDevice* dev = new_device(NORMAL);
  g_autoptr(FpPrint) enrolled = NULL;
  g_autoptr(FpPrint) restored = NULL;
  g_autofree guint8* serialized = NULL;
  gsize length;
  GError* error = NULL;
  enrolled = fp_device_enroll_sync(dev, fp_print_new(dev), NULL, NULL, NULL, &error);
  g_assert_no_error(error);
  g_assert_cmpuint(captures, ==, 1);
  assert_raw(enrolled);
  g_assert_true(fp_print_serialize(enrolled, &serialized, &length, &error));
  g_assert_no_error(error);
  restored = fp_print_deserialize(serialized, length, &error);
  g_assert_no_error(error);
  assert_raw(restored);
  close_device(dev);
}

static void test_verify(void) {
  FpDevice* dev = new_device(NORMAL);
  g_autoptr(FpPrint) reference = fp_print_new(dev);
  g_autoptr(FpPrint) scanned = NULL;
  GError* error = NULL;
  gboolean matched = FALSE;
  /* Deliberately unrelated data: the stub must not decode or compare it. */
  fpi_print_set_type(reference, FPI_PRINT_RAW);
  g_object_set(reference, "fpi-data", g_variant_new_string("not a raw template"), NULL);
  g_assert_true(fp_device_verify_sync(dev, reference, NULL, NULL, NULL, &matched, &scanned, &error));
  g_assert_no_error(error);
  g_assert_true(matched);
  g_assert_cmpuint(captures, ==, 1);
  assert_raw(scanned);
  close_device(dev);
}

static void test_identify(void) {
  FpDevice* dev = new_device(NORMAL);
  g_autoptr(GPtrArray) gallery = g_ptr_array_new_with_free_func(g_object_unref);
  g_autoptr(FpPrint) match = NULL;
  g_autoptr(FpPrint) scanned = NULL;
  GError* error = NULL;
  g_assert_true(fp_device_identify_sync(dev, gallery, NULL, NULL, NULL, &match, &scanned, &error));
  g_assert_no_error(error);
  g_assert_null(match);
  assert_raw(scanned);
  g_clear_object(&scanned);
  g_ptr_array_add(gallery, fp_print_new(dev));
  g_ptr_array_add(gallery, fp_print_new(dev));
  g_assert_true(fp_device_identify_sync(dev, gallery, NULL, NULL, NULL, &match, &scanned, &error));
  g_assert_no_error(error);
  g_assert_true(match == g_ptr_array_index(gallery, 0));
  assert_raw(scanned);
  close_device(dev);
}

static void test_failure(gconstpointer data) {
  Scenario test_scenario = GPOINTER_TO_INT(data);
  FpDevice* dev = new_device(test_scenario);
  g_autoptr(FpPrint) reference = fp_print_new(dev);
  g_autoptr(FpPrint) scanned = NULL;
  g_autoptr(GCancellable) cancellable = g_cancellable_new();
  g_autoptr(GError) error = NULL;
  gboolean matched = FALSE;
  fpi_print_set_type(reference, FPI_PRINT_RAW);
  g_object_set(reference, "fpi-data", g_variant_new_string("placeholder"), NULL);
  g_assert_false(fp_device_verify_sync(dev, reference, cancellable, NULL, NULL, &matched, &scanned, &error));
  g_assert_nonnull(error);
  g_assert_false(matched);
  g_assert_null(scanned);
  if (test_scenario == CANCEL_PENDING || test_scenario == CANCEL_TIMER) {
    g_assert_error(error, G_IO_ERROR, G_IO_ERROR_CANCELLED);
  } else {
    g_assert_error(error, FP_DEVICE_ERROR, FP_DEVICE_ERROR_PROTO);
  }
  if (test_scenario == ZERO_POLL_WRITE || test_scenario == SHORT_POLL_WRITE) {
    g_assert_cmpuint(polls, ==, 1);
    g_assert_cmpuint(status_reads, ==, 0);
    g_assert_cmpuint(captures, ==, 0);
    g_assert_cmpuint(frame_reads, ==, 0);
  } else if (test_scenario == ZERO_CAPTURE_WRITE || test_scenario == SHORT_CAPTURE_WRITE) {
    g_assert_cmpuint(status_reads, ==, 1);
    g_assert_cmpuint(captures, ==, 1);
    g_assert_cmpuint(frame_reads, ==, 0);
  } else if (test_scenario == EMPTY_STATUS || test_scenario == SHORT_STATUS || test_scenario == OVERSIZED_STATUS || test_scenario == BAD_STATUS_HEADER || test_scenario == BAD_STATUS_TYPE) {
    g_assert_cmpuint(polls, ==, 1);
    g_assert_cmpuint(status_reads, ==, 1);
    g_assert_cmpuint(captures, ==, 0);
    g_assert_cmpuint(frame_reads, ==, 0);
  }
  close_device(dev);
}

static void test_wait_then_ready(void) {
  FpDevice* dev = new_device(WAIT_THEN_READY);
  g_autoptr(FpPrint) enrolled = NULL;
  g_autoptr(GError) error = NULL;
  enrolled = fp_device_enroll_sync(dev, fp_print_new(dev), NULL, NULL, NULL, &error);
  g_assert_no_error(error);
  assert_raw(enrolled);
  g_assert_cmpuint(polls, ==, 3);
  g_assert_cmpuint(status_reads, ==, 3);
  g_assert_cmpuint(captures, ==, 1);
  close_device(dev);
}

static FpPrint* new_reference(FpDevice* dev, const guint8* bytes) {
  FpPrint* print = fp_print_new(dev);
  fpi_print_set_type(print, FPI_PRINT_RAW);
  g_object_set(print, "fpi-data", g_variant_new("(s@ay)", "reference-v1", g_variant_new_fixed_array(G_VARIANT_TYPE_BYTE, bytes, 8, 1)), NULL);
  return print;
}

static void test_usb_error_and_restart(gconstpointer data) {
  Scenario test_scenario = GPOINTER_TO_INT(data);
  FpDevice* dev = new_device(test_scenario);
  g_autoptr(FpPrint) reference = new_reference(dev, expected);
  g_autoptr(FpPrint) scanned = NULL;
  g_autoptr(GError) error = NULL;
  gboolean matched = FALSE;

  g_assert_false(fp_device_verify_sync(dev, reference, NULL, NULL, NULL, &matched, &scanned, &error));
  g_assert_error(error, G_IO_ERROR, G_IO_ERROR_FAILED);
  g_assert_cmpstr(error->message, ==, "Simulated USB failure");
  g_assert_false(matched);
  g_assert_null(scanned);
  g_assert_cmpuint(usb_failures, ==, 1);
  g_assert_cmpuint(action_completions, ==, 1);
  g_assert_cmpuint(algorithm_calls, ==, 0);
  g_assert_cmpuint(polls, ==, test_scenario == USB_ERROR ? 0 : 1);
  g_assert_cmpuint(status_reads, ==, test_scenario == USB_ERROR || test_scenario == USB_STATUS_ERROR ? 0 : 1);
  g_assert_cmpuint(captures, ==, test_scenario == USB_PARTIAL_FRAME_ERROR ? 1 : 0);
  g_assert_cmpuint(frame_reads, ==, test_scenario == USB_PARTIAL_FRAME_ERROR ? 1 : 0);
  g_assert_cmpuint(frame_offset, ==, test_scenario == USB_PARTIAL_FRAME_ERROR ? 4096 : 0);
  assert_capture_idle(dev);

  scenario = NORMAL;
  expected[0] ^= 0xff;
  g_clear_error(&error);
  g_assert_true(fp_device_verify_sync(dev, reference, NULL, NULL, NULL, &matched, &scanned, &error));
  g_assert_no_error(error);
  g_assert_true(matched);
  assert_raw(scanned);
  g_assert_cmpuint(usb_failures, ==, 1);
  g_assert_cmpuint(action_completions, ==, 2);
  g_assert_cmpuint(algorithm_calls, ==, 1);
  close_device(dev);
}

static void test_cancel_and_restart(gconstpointer data) {
  Scenario test_scenario = GPOINTER_TO_INT(data);
  FpDevice* dev = new_device(test_scenario);
  g_autoptr(FpPrint) reference = new_reference(dev, expected);
  g_autoptr(FpPrint) scanned = NULL;
  g_autoptr(GCancellable) cancellable = g_cancellable_new();
  g_autoptr(GError) error = NULL;
  gboolean matched = FALSE;

  g_assert_false(fp_device_verify_sync(dev, reference, cancellable, NULL, NULL, &matched, &scanned, &error));
  g_assert_error(error, G_IO_ERROR, G_IO_ERROR_CANCELLED);
  g_assert_false(matched);
  g_assert_null(scanned);
  g_assert_cmpuint(action_completions, ==, 1);
  g_assert_cmpuint(algorithm_calls, ==, 0);
  if (test_scenario == CANCEL_TIMER) {
    g_assert_cmpuint(polls, ==, 1);
    g_assert_cmpuint(captures, ==, 0);
    g_assert_cmpuint(frame_reads, ==, 0);
    g_assert_cmpuint(frame_offset, ==, 0);
  } else {
    g_assert_cmpuint(captures, ==, 1);
    g_assert_cmpuint(frame_reads, ==, test_scenario == CANCEL_PARTIAL_FRAME ? 1 : 3);
    g_assert_cmpuint(frame_offset, ==, test_scenario == CANCEL_PARTIAL_FRAME ? 4096 : FT9362_RAW_FRAME_SIZE);
  }
  assert_capture_idle(dev);

  guint previous_polls = polls;
  scenario = test_scenario == CANCEL_TIMER ? WAIT_THEN_READY : NORMAL;
  status_reads = 0;
  gint64 restart_time = g_get_monotonic_time();
  for (gsize i = 0; i < sizeof(expected); i++) {
    expected[i] ^= 0xff;
  }
  g_clear_error(&error);
  g_assert_true(fp_device_verify_sync(dev, reference, NULL, NULL, NULL, &matched, &scanned, &error));
  g_assert_no_error(error);
  g_assert_true(matched);
  assert_raw(scanned);
  g_assert_cmpuint(captures, ==, test_scenario == CANCEL_TIMER ? 1 : 2);
  g_assert_cmpuint(algorithm_calls, ==, 1);
  g_assert_cmpuint(action_completions, ==, 2);
  if (test_scenario == CANCEL_TIMER) {
    g_assert_cmpint(g_get_monotonic_time() - restart_time, >=, 50 * G_TIME_SPAN_MILLISECOND);
    g_assert_cmpuint(polls, ==, previous_polls + 3);
    g_assert_cmpuint(status_reads, ==, 3);
  }
  assert_capture_idle(dev);
  while (g_main_context_iteration(NULL, FALSE)) {
  }
  g_assert_cmpuint(action_completions, ==, 2);
  g_assert_cmpuint(algorithm_calls, ==, 1);
  close_device(dev);
}

static void test_custom_template(void) {
  FpDevice* dev = new_device(CUSTOM_TEMPLATE);
  g_autoptr(GError) error = NULL;
  g_autoptr(FpPrint) print = fp_device_enroll_sync(dev, fp_print_new(dev), NULL, NULL, NULL, &error);
  g_autoptr(GVariant) data = NULL;
  g_autoptr(GVariant) bytes = NULL;
  const gchar* tag;
  gsize size;
  g_assert_no_error(error);
  g_object_get(print, "fpi-data", &data, NULL);
  g_variant_get(data, "(&s@ay)", &tag, &bytes);
  const guint8* value = g_variant_get_fixed_array(bytes, &size, 1);
  g_assert_cmpstr(tag, ==, "FT9362-TEST-v1");
  g_assert_cmpmem(value, size, expected, 8);
  close_device(dev);
}

static void test_algorithm_verify(gconstpointer data) {
  Scenario test_scenario = GPOINTER_TO_INT(data);
  FpDevice* dev = new_device(test_scenario);
  g_autoptr(FpPrint) reference = new_reference(dev, expected);
  g_autoptr(FpPrint) scanned = NULL;
  g_autoptr(GError) error = NULL;
  gboolean matched = FALSE;
  gboolean success = fp_device_verify_sync(dev, reference, NULL, NULL, NULL, &matched, &scanned, &error);
  if (test_scenario == ALGORITHM_ERROR) {
    g_assert_false(success);
    g_assert_error(error, FP_DEVICE_ERROR, FP_DEVICE_ERROR_DATA_INVALID);
    g_assert_false(matched);
    g_assert_null(scanned);
  } else {
    g_assert_true(success);
    g_assert_no_error(error);
    g_assert_cmpint(matched, ==, test_scenario == OPTIONAL_SCAN);
    if (test_scenario == OPTIONAL_SCAN) {
      g_assert_null(scanned);
    } else {
      assert_raw(scanned);
    }
  }
  close_device(dev);
}

static void test_algorithm_identify(gconstpointer data) {
  Scenario test_scenario = GPOINTER_TO_INT(data);
  FpDevice* dev = new_device(test_scenario);
  g_autoptr(GPtrArray) gallery = g_ptr_array_new_with_free_func(g_object_unref);
  g_autoptr(FpPrint) match = NULL;
  g_autoptr(FpPrint) scanned = NULL;
  g_autoptr(GError) error = NULL;
  g_ptr_array_add(gallery, new_reference(dev, expected));
  g_ptr_array_add(gallery, new_reference(dev, expected + 8));
  gboolean success = fp_device_identify_sync(dev, gallery, NULL, NULL, NULL, &match, &scanned, &error);
  if (test_scenario == IDENTIFY_INVALID_INDEX) {
    g_assert_false(success);
    g_assert_error(error, FP_DEVICE_ERROR, FP_DEVICE_ERROR_GENERAL);
    g_assert_null(match);
    g_assert_null(scanned);
  } else {
    g_assert_true(success);
    g_assert_no_error(error);
    if (test_scenario == IDENTIFY_SECOND) {
      g_assert_true(match == g_ptr_array_index(gallery, 1));
    } else {
      g_assert_null(match);
    }
    assert_raw(scanned);
  }
  close_device(dev);
}

int main(int argc, char** argv) {
  g_test_init(&argc, &argv, NULL);
  g_autoptr(GUsbDevice) usb = g_object_new(G_USB_TYPE_DEVICE, NULL);
  test_usb = usb;
  g_test_add_func("/focaltech-raw/enroll-roundtrip", test_enroll);
  g_test_add_func("/focaltech-raw/verify-bypass", test_verify);
  g_test_add_func("/focaltech-raw/identify", test_identify);
  g_test_add_data_func("/focaltech-raw/empty-frame", GINT_TO_POINTER(EMPTY_FRAME), test_failure);
  g_test_add_data_func("/focaltech-raw/oversized-frame", GINT_TO_POINTER(OVERSIZED_FRAME), test_failure);
  g_test_add_data_func("/focaltech-raw/usb-error", GINT_TO_POINTER(USB_ERROR), test_usb_error_and_restart);
  g_test_add_data_func("/focaltech-raw/usb-status-error", GINT_TO_POINTER(USB_STATUS_ERROR), test_usb_error_and_restart);
  g_test_add_data_func("/focaltech-raw/usb-capture-error", GINT_TO_POINTER(USB_CAPTURE_ERROR), test_usb_error_and_restart);
  g_test_add_data_func("/focaltech-raw/usb-partial-frame-error", GINT_TO_POINTER(USB_PARTIAL_FRAME_ERROR), test_usb_error_and_restart);
  g_test_add_data_func("/focaltech-raw/claim-error", GINT_TO_POINTER(CLAIM_ERROR), test_interface_error);
  g_test_add_data_func("/focaltech-raw/release-error", GINT_TO_POINTER(RELEASE_ERROR), test_interface_error);
  g_test_add_data_func("/focaltech-raw/cancel-transfer", GINT_TO_POINTER(CANCEL_PENDING), test_failure);
  g_test_add_data_func("/focaltech-raw/cancel-timer", GINT_TO_POINTER(CANCEL_TIMER), test_failure);
  g_test_add_data_func("/focaltech-raw/zero-poll-write", GINT_TO_POINTER(ZERO_POLL_WRITE), test_failure);
  g_test_add_data_func("/focaltech-raw/short-poll-write", GINT_TO_POINTER(SHORT_POLL_WRITE), test_failure);
  g_test_add_data_func("/focaltech-raw/zero-capture-write", GINT_TO_POINTER(ZERO_CAPTURE_WRITE), test_failure);
  g_test_add_data_func("/focaltech-raw/short-capture-write", GINT_TO_POINTER(SHORT_CAPTURE_WRITE), test_failure);
  g_test_add_data_func("/focaltech-raw/empty-status", GINT_TO_POINTER(EMPTY_STATUS), test_failure);
  g_test_add_data_func("/focaltech-raw/short-status", GINT_TO_POINTER(SHORT_STATUS), test_failure);
  g_test_add_data_func("/focaltech-raw/oversized-status", GINT_TO_POINTER(OVERSIZED_STATUS), test_failure);
  g_test_add_data_func("/focaltech-raw/bad-status-header", GINT_TO_POINTER(BAD_STATUS_HEADER), test_failure);
  g_test_add_data_func("/focaltech-raw/bad-status-type", GINT_TO_POINTER(BAD_STATUS_TYPE), test_failure);
  g_test_add_func("/focaltech-raw/wait-then-ready", test_wait_then_ready);
  g_test_add_func("/focaltech-raw/algorithm/custom-template", test_custom_template);
  g_test_add_data_func("/focaltech-raw/algorithm/verify-no-match", GINT_TO_POINTER(VERIFY_NO_MATCH), test_algorithm_verify);
  g_test_add_data_func("/focaltech-raw/algorithm/error", GINT_TO_POINTER(ALGORITHM_ERROR), test_algorithm_verify);
  g_test_add_data_func("/focaltech-raw/algorithm/optional-scan", GINT_TO_POINTER(OPTIONAL_SCAN), test_algorithm_verify);
  g_test_add_data_func("/focaltech-raw/algorithm/identify-second", GINT_TO_POINTER(IDENTIFY_SECOND), test_algorithm_identify);
  g_test_add_data_func("/focaltech-raw/algorithm/identify-no-match", GINT_TO_POINTER(IDENTIFY_NO_MATCH), test_algorithm_identify);
  g_test_add_data_func("/focaltech-raw/algorithm/invalid-index", GINT_TO_POINTER(IDENTIFY_INVALID_INDEX), test_algorithm_identify);
  g_test_add_data_func("/focaltech-raw/cancel-partial-frame-and-restart", GINT_TO_POINTER(CANCEL_PARTIAL_FRAME), test_cancel_and_restart);
  g_test_add_data_func("/focaltech-raw/cancel-successful-frame-and-restart", GINT_TO_POINTER(CANCEL_SUCCESSFUL_FRAME), test_cancel_and_restart);
  g_test_add_data_func("/focaltech-raw/cancel-timer-and-restart", GINT_TO_POINTER(CANCEL_TIMER), test_cancel_and_restart);
  return g_test_run();
}
