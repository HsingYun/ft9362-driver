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
  CONTINUOUS_CAPTURE,
  EXTENDED_NOT_READY,
  BAD_STATUS_BCC,
  BAD_STATUS_LENGTH,
  UNKNOWN_STATUS,
  BAD_FRAME_HEADER,
  BAD_FRAME_BCC,
  BAD_FRAME_LENGTH,
  FRAME_REJECTED,
  FRAME_EMPTY_ACK,
  FRAME_WITHOUT_TRAILER,
  EXTENDED_WITHOUT_TRAILER,
  STATUS_WITHOUT_TRAILER,
  EXTRA_STATUS_BYTES,
  UNKNOWN_EXTENDED_STATUS,
  EXTENDED_REJECTED,
  BAD_EXTENDED_FRAME,
  CANCEL_EXTENDED_FRAME,
  REPLY_DEADLINE,
  COALESCED_REPLIES,
  SINGLE_READ_FRAME,
  SPLIT_HEADER,
  BAD_BRIDGE_VALUE,
  BAD_APP_VALUE,
  BAD_DIMENSION_A,
  BAD_DIMENSION_B,
  ZERO_EXTENDED_STATUS,
  CANCEL_STATUS_OUT,
  CANCEL_FRAME_OUT,
  CANCEL_AND_USB_ERROR,
  DRAIN_PARTIAL_TIMEOUT,
  STALE_HEALTH_REPLY,
} Scenario;

static Scenario scenario;
static guint8 expected[FT9362_RAW_FRAME_SIZE];
static gsize frame_offset;
static guint captures;
static guint extended_polls;
static guint extended_captures;
static guint health_queries;
static guint drain_reads;
static gboolean finger_down;
static gboolean frame_ready;
static GQueue replies = G_QUEUE_INIT;

typedef enum {
  POLL_REPLY,
  FRAME_REPLY,
  EXTENDED_STATUS_REPLY,
  EXTENDED_FRAME_REPLY,
  BRIDGE_REPLY,
  APP_REPLY,
  DIMENSION_A_REPLY,
  DIMENSION_B_REPLY
} ReplyKind;
typedef struct {
  guint8 bytes[FT9362_RAW_FRAME_SIZE + 2];
  gsize size;
  gsize offset;
  ReplyKind kind;
} Reply;

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
  g_assert_cmpmem(frame->data, frame->size, expected, sizeof(expected));
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
  g_assert_cmpmem(frame->data, frame->size, expected, sizeof(expected));
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

static guint8 test_bcc(const guint8* bytes, gsize size) {
  guint8 result = 0;
  for (gsize i = 0; i < size; i++) {
    result ^= bytes[i];
  }
  return result;
}

static void update_frame_checksum(void) {
  expected[12164] = test_bcc(expected + 1, 12163);
}

static void queue_reply(ReplyKind kind) {
  Reply* reply = g_new0(Reply, 1);
  reply->kind = kind;
  if (kind >= BRIDGE_REPLY) {
    const guint8 response[] = {2, 0, 3, 4, 0x55, 0xaa, 0xf8, 0};
    memcpy(reply->bytes, response, sizeof(response));
    reply->size = sizeof(response);
    if (kind == APP_REPLY) {
      reply->bytes[4] = 0xa5;
      reply->bytes[5] = 0x5a;
    } else if (kind == DIMENSION_A_REPLY || kind == DIMENSION_B_REPLY) {
      reply->bytes[2] = 2;
      reply->bytes[4] = kind == DIMENSION_A_REPLY ? 0x28 : 0x98;
      reply->size = 7;
    }
    if ((scenario == BAD_BRIDGE_VALUE && kind == BRIDGE_REPLY) || (scenario == BAD_APP_VALUE && kind == APP_REPLY) || (scenario == BAD_DIMENSION_A && kind == DIMENSION_A_REPLY) || (scenario == BAD_DIMENSION_B && kind == DIMENSION_B_REPLY)) {
      reply->bytes[4] ^= 1;
    }
    reply->bytes[reply->size - 2] = test_bcc(reply->bytes + 1, reply->size - 3);
  } else if (kind == FRAME_REPLY || kind == EXTENDED_FRAME_REPLY) {
    memcpy(reply->bytes, expected, sizeof(expected));
    reply->size = sizeof(expected);
    if (kind == EXTENDED_FRAME_REPLY) {
      for (gsize i = 4; i < 12164; i++) {
        reply->bytes[i] ^= 0xa5;
      }
      reply->bytes[12164] = test_bcc(reply->bytes + 1, 12163);
    }
    if (scenario == BAD_FRAME_HEADER) {
      reply->bytes[0] = 0xff;
    } else if (scenario == BAD_FRAME_LENGTH) {
      reply->bytes[1] = 0xff;
    } else if (scenario == BAD_FRAME_BCC || (scenario == BAD_EXTENDED_FRAME && kind == EXTENDED_FRAME_REPLY)) {
      reply->bytes[12164] ^= 1;
    } else if (scenario == FRAME_REJECTED || scenario == FRAME_EMPTY_ACK) {
      const guint8 rejected[] = {2, 0, 1, 9, 8, 0x93};
      memcpy(reply->bytes, rejected, sizeof(rejected));
      reply->size = sizeof(rejected);
      if (scenario == FRAME_EMPTY_ACK) {
        reply->bytes[3] = 4;
        reply->bytes[4] = 5;
      }
    } else if (scenario == FRAME_WITHOUT_TRAILER || (scenario == EXTENDED_WITHOUT_TRAILER && kind == EXTENDED_FRAME_REPLY)) {
      reply->size--;
    }
  } else {
    const guint8 response[] = {2, 0, 2, 4, 0, 6, 0xf8};
    memcpy(reply->bytes, response, sizeof(response));
    reply->size = sizeof(response);
    reply->bytes[4] = kind == POLL_REPLY ? frame_ready : (finger_down ? 5 : 6);
    if (scenario == UNKNOWN_STATUS && kind == POLL_REPLY) {
      reply->bytes[4] = 7;
    } else if (scenario == UNKNOWN_EXTENDED_STATUS && kind == EXTENDED_STATUS_REPLY) {
      reply->bytes[4] = 7;
    }
    if (scenario == ZERO_EXTENDED_STATUS && kind == EXTENDED_STATUS_REPLY) {
      reply->bytes[4] = 0;
    }
    reply->bytes[5] = test_bcc(reply->bytes + 1, 4);
    if (kind == POLL_REPLY) {
      if (scenario == SHORT_STATUS) {
        reply->size = 5;
      } else if (scenario == BAD_STATUS_HEADER) {
        reply->bytes[0] = 0xff;
      } else if (scenario == BAD_STATUS_TYPE) {
        reply->bytes[3] = 9;
        reply->bytes[5] = test_bcc(reply->bytes + 1, 4);
      } else if (scenario == BAD_STATUS_BCC) {
        reply->bytes[5] ^= 1;
      } else if (scenario == BAD_STATUS_LENGTH) {
        reply->bytes[2] = 3;
      } else if (scenario == STATUS_WITHOUT_TRAILER) {
        reply->size--;
      } else if (scenario == EXTRA_STATUS_BYTES) {
        reply->size++;
      } else if (scenario == COALESCED_REPLIES) {
        memcpy(reply->bytes + 7, reply->bytes, 7);
        reply->size = 14;
      }
    } else if (scenario == EXTENDED_REJECTED) {
      const guint8 rejected[] = {2, 0, 1, 9, 8, 0x93};
      memcpy(reply->bytes, rejected, sizeof(rejected));
      reply->size = sizeof(rejected);
    }
  }
  g_queue_push_tail(&replies, reply);
}

static gboolean deliver(gpointer data) {
  Pending* pending = data;
  FpiUsbTransfer* transfer = pending->transfer;
  Reply* reply = g_queue_peek_head(&replies);
  GError* error = NULL;
  gboolean out = transfer->endpoint == 0x03;
  gboolean normal_capture = out && transfer->buffer[3] == 0x81;

  if (scenario == USB_ERROR || (scenario == USB_STATUS_ERROR && !out && reply && reply->kind == POLL_REPLY) || (scenario == USB_CAPTURE_ERROR && normal_capture) ||
      ((scenario == USB_PARTIAL_FRAME_ERROR || scenario == CANCEL_AND_USB_ERROR) && !out && reply && reply->kind == FRAME_REPLY && reply->offset == 4096)) {
    usb_failures++;
    error = g_error_new_literal(G_IO_ERROR, G_IO_ERROR_FAILED, "Simulated USB failure");
  } else if (scenario == DRAIN_PARTIAL_TIMEOUT && !out && reply) {
    reply->offset += 100;
    error = g_error_new_literal(G_USB_DEVICE_ERROR, G_USB_DEVICE_ERROR_TIMED_OUT, "Timeout hides partial bytes");
    scenario = NORMAL;
  } else if (out) {
    ReplyKind kind;
    if (normal_capture) {
      g_assert_cmpmem(transfer->buffer, transfer->length, expected_capture_command, sizeof(expected_capture_command));
      kind = FRAME_REPLY;
      captures++;
      frame_offset = 0;
    } else if (transfer->buffer[3] == 0x34 || (transfer->buffer[3] == 0x80 && transfer->buffer[4] != 2)) {
      const guint8 bridge[] = {2, 0, 1, 0x34, 0x35};
      const guint8 app[] = {2, 0, 3, 0x80, 1, 2, 0x80};
      const guint8 a[] = {2, 0, 3, 0x80, 3, 1, 0x81};
      const guint8 b[] = {2, 0, 3, 0x80, 4, 1, 0x86};
      if (transfer->buffer[3] == 0x34) {
        g_assert_cmpmem(transfer->buffer, transfer->length, bridge, sizeof(bridge));
        kind = BRIDGE_REPLY;
      } else if (transfer->buffer[4] == 1) {
        g_assert_cmpmem(transfer->buffer, transfer->length, app, sizeof(app));
        kind = APP_REPLY;
      } else if (transfer->buffer[4] == 3) {
        g_assert_cmpmem(transfer->buffer, transfer->length, a, sizeof(a));
        kind = DIMENSION_A_REPLY;
      } else {
        g_assert_cmpmem(transfer->buffer, transfer->length, b, sizeof(b));
        kind = DIMENSION_B_REPLY;
      }
      health_queries++;
    } else if (transfer->buffer[3] == 0x80) {
      g_assert_cmpmem(transfer->buffer, transfer->length, expected_poll_command, sizeof(expected_poll_command));
      kind = POLL_REPLY;
      polls++;
      g_assert_cmpuint(polls, <, 20);

    } else {
      static const guint8 poll[] = {2, 0, 3, 0x82, 0x73, 1, 0xf3};
      static const guint8 image[] = {2, 0, 3, 0x82, 0x76, 1, 0xf6};
      if (transfer->buffer[4] == 0x73) {
        g_assert_cmpmem(transfer->buffer, transfer->length, poll, sizeof(poll));
        kind = EXTENDED_STATUS_REPLY;
        extended_polls++;
        if (scenario == WAIT_THEN_READY && extended_polls >= 3) {
          frame_ready = TRUE;
          finger_down = TRUE;
        }
      } else {
        g_assert_cmpmem(transfer->buffer, transfer->length, image, sizeof(image));
        kind = EXTENDED_FRAME_REPLY;
        extended_captures++;
        g_assert_true(finger_down);
      }
    }
    transfer->actual_length = transfer->length;
    if ((scenario == ZERO_POLL_WRITE && kind == POLL_REPLY) || (scenario == ZERO_CAPTURE_WRITE && kind == FRAME_REPLY)) {
      transfer->actual_length = 0;
    } else if ((scenario == SHORT_POLL_WRITE && kind == POLL_REPLY) || (scenario == SHORT_CAPTURE_WRITE && kind == FRAME_REPLY)) {
      transfer->actual_length--;
    } else {
      if (scenario == STALE_HEALTH_REPLY && kind == BRIDGE_REPLY) {
        queue_reply(FRAME_REPLY);
      }
      queue_reply(kind);
    }
  } else if (!reply) {
    error = g_error_new_literal(G_USB_DEVICE_ERROR, G_USB_DEVICE_ERROR_TIMED_OUT, "No queued USB response");
  } else {
    gboolean image = reply->kind == FRAME_REPLY || reply->kind == EXTENDED_FRAME_REPLY;
    if (reply->kind == POLL_REPLY) {
      status_reads++;
    } else if (reply->kind == FRAME_REPLY) {
      frame_reads++;
    }
    gsize chunk = scenario == SINGLE_READ_FRAME ? FT9362_RAW_FRAME_SIZE : 4096;
    if (scenario == SPLIT_HEADER && reply->offset < 3) {
      chunk = 1;
    }
    gsize size = MIN((gsize)transfer->length, MIN(chunk, reply->size - reply->offset));
    if ((scenario == EMPTY_FRAME && image) || (scenario == EMPTY_STATUS && reply->kind == POLL_REPLY)) {
      size = 0;
    }
    memcpy(transfer->buffer, reply->bytes + reply->offset, size);
    transfer->actual_length = size;
    reply->offset += size;
    if (reply->kind == FRAME_REPLY) {
      frame_offset = reply->offset;
    }
    if (scenario == OVERSIZED_STATUS && reply->kind == POLL_REPLY) {
      transfer->actual_length = transfer->length + 1;
    } else if (scenario == OVERSIZED_FRAME && image && reply->offset > 4096) {
      transfer->actual_length = transfer->length + 1;
    }
    gboolean final_normal_frame = reply->kind == FRAME_REPLY && reply->offset == reply->size;
    if (reply->offset == reply->size) {
      if (reply->kind == FRAME_REPLY) {
        frame_ready = FALSE;
      } else if (reply->kind == EXTENDED_FRAME_REPLY) {
        frame_ready = scenario != EXTENDED_NOT_READY;
        if (scenario == EXTENDED_NOT_READY) {
          finger_down = FALSE;
        }
      } else if (reply->kind == EXTENDED_STATUS_REPLY && reply->bytes[4] == 6 && (scenario == CANCEL_TIMER || scenario == EXTENDED_NOT_READY)) {
        g_idle_add_full(G_PRIORITY_DEFAULT_IDLE, cancel_cb, g_object_ref(pending->cancellable), g_object_unref);
      }
      g_free(g_queue_pop_head(&replies));
    }
    if (scenario == CANCEL_SUCCESSFUL_FRAME && final_normal_frame) {
      g_cancellable_cancel(pending->cancellable);
    }
    if (scenario == REPLY_DEADLINE && image) {
      FPI_DEVICE_FOCALTECH0752(transfer->device)->transaction.reply_deadline_us = g_get_monotonic_time() - 1;
    }
  }

  if (error != NULL) {
    transfer->actual_length = -1; /* GUsb finish discards partial byte counts on errors. */
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
  g_assert_null(cancellable);
  GCancellable* action_cancellable = fpi_device_get_cancellable(transfer->device);
  if (transfer->endpoint == 0x03) {
    g_assert_cmpuint(timeout, ==, 1000);
    g_assert_true(transfer->short_is_error);
  } else {
    g_assert_cmpuint(transfer->endpoint, ==, 0x81);
    g_assert_cmpuint(transfer->length % 64, ==, 0);
    g_assert_cmpuint(timeout, >, 0);
    const Ft9362Command* command = FPI_DEVICE_FOCALTECH0752(transfer->device)->transaction.command;
    g_assert_cmpuint(timeout, <=, command ? command->timeout_ms : 200);
    if (!command) {
      drain_reads++;
      g_assert_cmpuint(transfer->length, >=, FT9362_RAW_FRAME_SIZE);
    }
    g_assert_false(transfer->short_is_error);
  }
  pending->transfer = transfer;
  pending->cancellable = g_object_ref(action_cancellable);
  pending->callback = callback;
  pending->user_data = user_data;
  pending_transfers++;
  Reply* reply = g_queue_peek_head(&replies);
  gboolean cancel_out = transfer->endpoint == 0x03 && ((scenario == CANCEL_STATUS_OUT && transfer->buffer[3] == 0x80 && transfer->buffer[4] == 2) || (scenario == CANCEL_FRAME_OUT && transfer->buffer[3] == 0x81));
  if (scenario == CANCEL_PENDING || cancel_out || ((scenario == CANCEL_PARTIAL_FRAME || scenario == CANCEL_EXTENDED_FRAME || scenario == CANCEL_AND_USB_ERROR) && transfer->endpoint == 0x81 && reply && reply->offset == 4096)) {
    g_cancellable_cancel(action_cancellable);
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
  extended_polls = 0;
  extended_captures = 0;
  health_queries = 0;
  drain_reads = 0;
  g_queue_clear_full(&replies, g_free);
  finger_down = test_scenario != WAIT_THEN_READY && test_scenario != CANCEL_TIMER;
  frame_ready = finger_down && test_scenario != CONTINUOUS_CAPTURE && test_scenario != EXTENDED_WITHOUT_TRAILER && test_scenario != UNKNOWN_EXTENDED_STATUS && test_scenario != EXTENDED_REJECTED && test_scenario != BAD_EXTENDED_FRAME && test_scenario != CANCEL_EXTENDED_FRAME &&
                test_scenario != EXTENDED_NOT_READY;
  g_assert_cmpuint(pending_transfers, ==, 0);
  action_completions = 0;
  algorithm_calls = 0;
  usb_failures = 0;
  claims = 0;
  releases = 0;
  for (gsize i = 0; i < FT9362_RAW_FRAME_SIZE; i++) {
    expected[i] = (i * 37 + 11) & 0xff;
  }
  expected[0] = 2;
  expected[1] = 0x2f;
  expected[2] = 0x81;
  expected[3] = 4;
  update_frame_checksum();
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
  g_assert_false(FPI_DEVICE_FOCALTECH0752(dev)->transaction.transfer_pending);
  g_assert_false(FPI_DEVICE_FOCALTECH0752(dev)->transaction.active);
  g_assert_null(FPI_DEVICE_FOCALTECH0752(dev)->capture.machine);
  g_assert_cmpuint(FPI_DEVICE_FOCALTECH0752(dev)->transaction.bytes_received, ==, 0);
  const guint8 cleared_frame[FT9362_RAW_FRAME_SIZE] = {0};
  g_assert_cmpmem(FPI_DEVICE_FOCALTECH0752(dev)->transaction.reply, FT9362_RAW_FRAME_SIZE, cleared_frame, sizeof(cleared_frame));
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
  g_assert_null(scanned);
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
  g_assert_null(scanned);
  close_device(dev);
}

static void test_matching_scan(gconstpointer data) {
  FpDevice* dev = new_device(NORMAL);
  g_autoptr(GError) error = NULL;
  g_autoptr(FpPrint) reference = fp_device_enroll_sync(dev, fp_print_new(dev), NULL, NULL, NULL, &error);
  g_autoptr(FpPrint) scanned = NULL;
  g_assert_no_error(error);
  if (GPOINTER_TO_INT(data) == FPI_DEVICE_ACTION_VERIFY) {
    gboolean matched = FALSE;
    g_assert_true(fp_device_verify_sync(dev, reference, NULL, NULL, NULL, &matched, &scanned, &error));
    g_assert_true(matched);
  } else {
    g_autoptr(GPtrArray) gallery = g_ptr_array_new_with_free_func(g_object_unref);
    g_autoptr(FpPrint) match = NULL;
    g_ptr_array_add(gallery, g_object_ref(reference));
    g_assert_true(fp_device_identify_sync(dev, gallery, NULL, NULL, NULL, &match, &scanned, &error));
    g_assert_true(match == reference);
  }
  g_assert_no_error(error);
  assert_raw(scanned);
  g_assert_true(fp_print_equal(scanned, reference));
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
    if (test_scenario == FRAME_WITHOUT_TRAILER) {
      g_assert_error(error, FP_DEVICE_ERROR, FP_DEVICE_ERROR_NOT_SUPPORTED);
    } else if (test_scenario == SHORT_STATUS || test_scenario == REPLY_DEADLINE) {
      g_assert_error(error, G_USB_DEVICE_ERROR, G_USB_DEVICE_ERROR_TIMED_OUT);
    } else {
      g_assert_error(error, FP_DEVICE_ERROR, FP_DEVICE_ERROR_PROTO);
    }
  }
  g_assert_cmpuint(algorithm_calls, ==, 0);
  g_assert_cmpuint(action_completions, ==, 1);
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
  g_assert_cmpuint(polls, ==, 1);
  g_assert_cmpuint(status_reads, ==, 1);
  g_assert_cmpuint(extended_polls, ==, 3);
  g_assert_cmpuint(captures, ==, 1);
  close_device(dev);
}

static FpPrint* new_reference(FpDevice* dev, const guint8* bytes) {
  FpPrint* print = fp_print_new(dev);
  fpi_print_set_type(print, FPI_PRINT_RAW);
  g_object_set(print, "fpi-data", g_variant_new("(s@ay)", "reference-v1", g_variant_new_fixed_array(G_VARIANT_TYPE_BYTE, bytes, 8, 1)), NULL);
  return print;
}

static void assert_restart_succeeds(FpDevice* dev, FpPrint* reference) {
  g_autoptr(GError) error = NULL;
  gboolean matched = FALSE;
  guint calls = algorithm_calls;
  scenario = NORMAL;
  g_assert_true(fp_device_verify_sync(dev, reference, NULL, NULL, NULL, &matched, NULL, &error));
  g_assert_no_error(error);
  g_assert_true(matched);
  g_assert_cmpuint(algorithm_calls, ==, calls + 1);
  g_assert_true(g_queue_is_empty(&replies));
  assert_capture_idle(dev);
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

  if (test_scenario == USB_STATUS_ERROR || test_scenario == USB_PARTIAL_FRAME_ERROR) {
    g_assert_false(g_queue_is_empty(&replies));
    guint previous_health = health_queries;
    assert_restart_succeeds(dev, reference);
    g_assert_cmpuint(health_queries, ==, previous_health + 4);
    close_device(dev);
    return;
  }
  g_assert_true(g_queue_is_empty(&replies));
  scenario = NORMAL;
  expected[4] ^= 0xff;
  update_frame_checksum();
  g_clear_error(&error);
  g_assert_true(fp_device_verify_sync(dev, reference, NULL, NULL, NULL, &matched, &scanned, &error));
  g_assert_no_error(error);
  g_assert_true(matched);
  g_assert_null(scanned);
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
    g_assert_cmpuint(polls, ==, 0);
    g_assert_cmpuint(captures, ==, 0);
    g_assert_cmpuint(frame_reads, ==, 0);
    g_assert_cmpuint(frame_offset, ==, 0);
  } else {
    g_assert_cmpuint(captures, ==, 1);
    g_assert_cmpuint(frame_reads, ==, 3);
    g_assert_cmpuint(frame_offset, ==, FT9362_RAW_FRAME_SIZE);
  }
  assert_capture_idle(dev);

  g_assert_true(g_queue_is_empty(&replies));
  guint previous_polls = polls;
  scenario = test_scenario == CANCEL_TIMER ? WAIT_THEN_READY : NORMAL;
  status_reads = 0;
  extended_polls = 0;
  gint64 restart_time = g_get_monotonic_time();
  for (gsize i = 4; i < 12164; i++) {
    expected[i] ^= 0xff;
  }
  update_frame_checksum();
  g_clear_error(&error);
  g_assert_true(fp_device_verify_sync(dev, reference, NULL, NULL, NULL, &matched, &scanned, &error));
  g_assert_no_error(error);
  g_assert_true(matched);
  g_assert_null(scanned);
  g_assert_cmpuint(captures, ==, test_scenario == CANCEL_TIMER ? 1 : 2);
  g_assert_cmpuint(algorithm_calls, ==, 1);
  g_assert_cmpuint(action_completions, ==, 2);
  if (test_scenario == CANCEL_TIMER) {
    g_assert_cmpint(g_get_monotonic_time() - restart_time, >=, 50 * G_TIME_SPAN_MILLISECOND);
    g_assert_cmpuint(polls, ==, previous_polls + 1);
    g_assert_cmpuint(status_reads, ==, 1);
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
      g_assert_null(scanned);
    } else {
      g_assert_null(match);
      assert_raw(scanned);
    }
  }
  close_device(dev);
}

static void test_continuous_capture(void) {
  FpDevice* dev = new_device(CONTINUOUS_CAPTURE);
  for (guint i = 0; i < 3; i++) {
    g_autoptr(GError) error = NULL;
    expected[4]++;
    expected[12165] ^= 0xff;
    update_frame_checksum();
    g_autoptr(FpPrint) print = fp_device_enroll_sync(dev, fp_print_new(dev), NULL, NULL, NULL, &error);
    g_assert_no_error(error);
    assert_raw(print);
    assert_capture_idle(dev);
    g_assert_true(finger_down);
    g_assert_false(frame_ready);
    g_assert_true(g_queue_is_empty(&replies));
    g_assert_cmpuint(captures, ==, i + 1);
    g_assert_cmpuint(extended_captures, ==, i + 1);
    g_assert_cmpuint(extended_polls, ==, i + 1);
    g_assert_cmpuint(polls, ==, 2 * (i + 1));
  }
  g_assert_cmpuint(algorithm_calls, ==, 3);
  close_device(dev);
}

static void test_response_delivery(gconstpointer data) {
  FpDevice* dev = new_device(GPOINTER_TO_INT(data));
  g_autoptr(GError) error = NULL;
  g_autoptr(FpPrint) print = fp_device_enroll_sync(dev, fp_print_new(dev), NULL, NULL, NULL, &error);
  g_assert_no_error(error);
  assert_raw(print);
  g_assert_true(g_queue_is_empty(&replies));
  g_assert_cmpuint(extended_captures, ==, scenario == EXTENDED_WITHOUT_TRAILER ? 1 : 0);
  g_assert_cmpuint(frame_reads, ==, scenario == SINGLE_READ_FRAME ? 1 : (scenario == SPLIT_HEADER ? 6 : 3));
  close_device(dev);
}

static void test_extended_cancel(gconstpointer data) {
  Scenario test_scenario = GPOINTER_TO_INT(data);
  FpDevice* dev = new_device(test_scenario);
  g_autoptr(FpPrint) reference = new_reference(dev, expected);
  g_autoptr(GError) error = NULL;
  g_autoptr(GCancellable) cancellable = g_cancellable_new();
  gboolean matched = FALSE;
  g_assert_false(fp_device_verify_sync(dev, reference, cancellable, NULL, NULL, &matched, NULL, &error));
  g_assert_error(error, G_IO_ERROR, G_IO_ERROR_CANCELLED);
  g_assert_cmpuint(algorithm_calls, ==, 0);
  g_assert_cmpuint(action_completions, ==, 1);
  g_assert_cmpuint(captures, ==, 0);
  g_assert_cmpuint(extended_captures, ==, 1);
  assert_capture_idle(dev);
  if (test_scenario == CANCEL_EXTENDED_FRAME) {
    g_assert_true(g_queue_is_empty(&replies));
    assert_restart_succeeds(dev, reference);
  } else {
    g_assert_true(g_queue_is_empty(&replies));
    g_assert_cmpuint(polls, ==, 2);
    g_assert_cmpuint(extended_polls, ==, 2);
  }
  close_device(dev);
}

/* Recovery tests preserve the simulated device queue across actions and reopen. */
static void test_initial_drain(gconstpointer data) {
  gboolean reopen = GPOINTER_TO_INT(data);
  FpDevice* dev = new_device(NORMAL);
  g_autoptr(GError) error = NULL;
  if (reopen) {
    g_autoptr(FpPrint) first = fp_device_enroll_sync(dev, fp_print_new(dev), NULL, NULL, NULL, &error);
    g_assert_no_error(error);
    g_assert_true(fp_device_close_sync(dev, NULL, &error));
    g_assert_no_error(error);
    g_assert_true(fp_device_open_sync(dev, NULL, &error));
    g_assert_no_error(error);
  }
  queue_reply(FRAME_REPLY);
  expected[4] ^= 1;
  update_frame_checksum();
  guint old_health = health_queries;
  guint old_drain = drain_reads;
  g_autoptr(FpPrint) print = fp_device_enroll_sync(dev, fp_print_new(dev), NULL, NULL, NULL, &error);
  g_assert_no_error(error);
  assert_raw(print);
  g_assert_cmpuint(health_queries, ==, old_health + 4);
  g_assert_cmpuint(drain_reads, ==, old_drain + 4); /* Three chunks, then timeout. */
  g_assert_false(FPI_DEVICE_FOCALTECH0752(dev)->needs_sync);
  g_assert_true(g_queue_is_empty(&replies));
  close_device(dev);
}

static void test_recovery_failure(gconstpointer data) {
  Scenario test_scenario = GPOINTER_TO_INT(data);
  FpDevice* dev = new_device(test_scenario);
  g_autoptr(GError) error = NULL;
  if (test_scenario == NORMAL) {
    for (guint i = 0; i < 5; i++) {
      queue_reply(POLL_REPLY);
    }
  } else if (test_scenario == DRAIN_PARTIAL_TIMEOUT) {
    queue_reply(FRAME_REPLY);
  }
  g_autoptr(FpPrint) print = fp_device_enroll_sync(dev, fp_print_new(dev), NULL, NULL, NULL, &error);
  g_assert_null(print);
  g_assert_error(error, FP_DEVICE_ERROR, FP_DEVICE_ERROR_PROTO);
  g_assert_cmpuint(algorithm_calls, ==, 0);
  g_assert_true(FPI_DEVICE_FOCALTECH0752(dev)->needs_sync);
  if (test_scenario == NORMAL) {
    g_assert_cmpuint(drain_reads, ==, 4);
    g_assert_cmpuint(health_queries, ==, 0);
  } else if (test_scenario == STALE_HEALTH_REPLY) {
    g_assert_cmpuint(frame_offset, ==, FT9362_RAW_FRAME_SIZE);
    g_assert_cmpuint(health_queries, ==, 1);
  }
  assert_capture_idle(dev);
  g_autoptr(FpPrint) reference = new_reference(dev, expected);
  assert_restart_succeeds(dev, reference);
  g_assert_false(FPI_DEVICE_FOCALTECH0752(dev)->needs_sync);
  close_device(dev);
}

static void test_cancel_boundary(gconstpointer data) {
  Scenario test_scenario = GPOINTER_TO_INT(data);
  FpDevice* dev = new_device(test_scenario);
  g_autoptr(GCancellable) cancellable = g_cancellable_new();
  g_autoptr(GError) error = NULL;
  g_autoptr(FpPrint) print = fp_device_enroll_sync(dev, fp_print_new(dev), cancellable, NULL, NULL, &error);
  g_assert_null(print);
  g_assert_error(error, G_IO_ERROR, G_IO_ERROR_CANCELLED);
  g_assert_cmpuint(algorithm_calls, ==, 0);
  g_assert_cmpuint(action_completions, ==, 1);
  assert_capture_idle(dev);
  gboolean dirty = test_scenario == CANCEL_AND_USB_ERROR || test_scenario == CANCEL_PENDING;
  g_assert_cmpint(FPI_DEVICE_FOCALTECH0752(dev)->needs_sync, ==, dirty);
  g_assert_cmpint(g_queue_is_empty(&replies), ==, test_scenario != CANCEL_AND_USB_ERROR);
  if (test_scenario == CANCEL_PENDING) {
    g_assert_cmpuint(health_queries, ==, 0);
  }
  guint previous_health = health_queries;
  g_autoptr(FpPrint) reference = new_reference(dev, expected);
  assert_restart_succeeds(dev, reference);
  g_assert_cmpuint(health_queries, ==, previous_health + (dirty ? 4 : 0));
  close_device(dev);
}

static void test_frame_delivery_failure(gconstpointer data) {
  FpiDeviceAction action = GPOINTER_TO_INT(data);
  FpDevice* dev = new_device(FRAME_WITHOUT_TRAILER);
  g_autoptr(FpPrint) reference = new_reference(dev, expected);
  g_autoptr(GPtrArray) gallery = g_ptr_array_new();
  g_ptr_array_add(gallery, reference);
  g_autoptr(GError) error = NULL;
  if (action == FPI_DEVICE_ACTION_ENROLL) {
    g_autoptr(FpPrint) print = fp_device_enroll_sync(dev, fp_print_new(dev), NULL, NULL, NULL, &error);
    g_assert_null(print);
  } else if (action == FPI_DEVICE_ACTION_VERIFY) {
    gboolean matched = TRUE;
    g_assert_false(fp_device_verify_sync(dev, reference, NULL, NULL, NULL, &matched, NULL, &error));
    g_assert_false(matched);
  } else {
    g_autoptr(FpPrint) match = NULL;
    g_assert_false(fp_device_identify_sync(dev, gallery, NULL, NULL, NULL, &match, NULL, &error));
    g_assert_null(match);
  }
  g_assert_error(error, FP_DEVICE_ERROR, FP_DEVICE_ERROR_NOT_SUPPORTED);
  g_assert_cmpuint(algorithm_calls, ==, 0);
  g_assert_cmpuint(action_completions, ==, 1);
  g_assert_false(FPI_DEVICE_FOCALTECH0752(dev)->needs_sync);
  g_assert_true(g_queue_is_empty(&replies));
  assert_capture_idle(dev);

  guint previous_health = health_queries;
  guint previous_drain = drain_reads;
  assert_restart_succeeds(dev, reference);
  g_assert_cmpuint(health_queries, ==, previous_health);
  g_assert_cmpuint(drain_reads, ==, previous_drain);
  close_device(dev);
}

static void test_error_recovery_boundary(gconstpointer data) {
  Scenario test_scenario = GPOINTER_TO_INT(data);
  FpDevice* dev = new_device(test_scenario);
  g_autoptr(FpPrint) reference = new_reference(dev, expected);
  g_autoptr(GError) error = NULL;
  gboolean matched = TRUE;
  g_assert_false(fp_device_verify_sync(dev, reference, NULL, NULL, NULL, &matched, NULL, &error));
  gboolean protocol_error = test_scenario == BAD_FRAME_BCC;
  FpDeviceError expected_code = protocol_error ? FP_DEVICE_ERROR_PROTO : FP_DEVICE_ERROR_DATA_INVALID;
  g_assert_error(error, FP_DEVICE_ERROR, expected_code);
  g_assert_false(matched);
  g_assert_cmpuint(algorithm_calls, ==, protocol_error ? 0 : 1);
  g_assert_cmpuint(action_completions, ==, 1);
  g_assert_cmpint(FPI_DEVICE_FOCALTECH0752(dev)->needs_sync, ==, protocol_error);
  g_assert_true(g_queue_is_empty(&replies));
  assert_capture_idle(dev);
  guint previous_health = health_queries;
  guint previous_drain = drain_reads;
  assert_restart_succeeds(dev, reference);
  g_assert_cmpuint(health_queries, ==, previous_health + (protocol_error ? 4 : 0));
  g_assert_cmpuint(drain_reads, ==, previous_drain + (protocol_error ? 1 : 0));
  close_device(dev);
}

static void test_ready_frame_without_contact(void) {
  FpDevice* dev = new_device(CANCEL_TIMER);
  frame_ready = TRUE;
  finger_down = FALSE;
  g_autoptr(GCancellable) cancellable = g_cancellable_new();
  g_autoptr(GError) error = NULL;
  g_autoptr(FpPrint) print = fp_device_enroll_sync(dev, fp_print_new(dev), cancellable, NULL, NULL, &error);
  g_assert_null(print);
  g_assert_error(error, G_IO_ERROR, G_IO_ERROR_CANCELLED);
  g_assert_cmpuint(captures, ==, 0);
  g_assert_cmpuint(extended_captures, ==, 0);
  g_assert_cmpuint(polls, ==, 0);
  g_assert_cmpuint(algorithm_calls, ==, 0);
  close_device(dev);
}

int main(int argc, char** argv) {
  g_test_init(&argc, &argv, NULL);
  g_autoptr(GUsbDevice) usb = g_object_new(G_USB_TYPE_DEVICE, NULL);
  test_usb = usb;
  g_test_add_func("/focaltech-raw/enroll-roundtrip", test_enroll);
  g_test_add_func("/focaltech-raw/verify-bypass", test_verify);
  g_test_add_func("/focaltech-raw/identify", test_identify);
  g_test_add_data_func("/focaltech-raw/verify-matching-scan", GINT_TO_POINTER(FPI_DEVICE_ACTION_VERIFY), test_matching_scan);
  g_test_add_data_func("/focaltech-raw/identify-matching-scan", GINT_TO_POINTER(FPI_DEVICE_ACTION_IDENTIFY), test_matching_scan);
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
  g_test_add_data_func("/focaltech-raw/cancel-partial-frame-finishes-transaction", GINT_TO_POINTER(CANCEL_PARTIAL_FRAME), test_cancel_and_restart);
  g_test_add_data_func("/focaltech-raw/cancel-successful-frame-and-restart", GINT_TO_POINTER(CANCEL_SUCCESSFUL_FRAME), test_cancel_and_restart);
  g_test_add_data_func("/focaltech-raw/cancel-timer-and-restart", GINT_TO_POINTER(CANCEL_TIMER), test_cancel_and_restart);
  g_test_add_func("/focaltech-raw/continuous-capture", test_continuous_capture);
  g_test_add_data_func("/focaltech-raw/status-without-trailer", GINT_TO_POINTER(STATUS_WITHOUT_TRAILER), test_response_delivery);
  g_test_add_data_func("/focaltech-raw/single-read-frame", GINT_TO_POINTER(SINGLE_READ_FRAME), test_response_delivery);
  g_test_add_data_func("/focaltech-raw/split-header", GINT_TO_POINTER(SPLIT_HEADER), test_response_delivery);
  g_test_add_data_func("/focaltech-raw/extended-cancel", GINT_TO_POINTER(CANCEL_EXTENDED_FRAME), test_extended_cancel);
  g_test_add_data_func("/focaltech-raw/extended-still-not-ready", GINT_TO_POINTER(EXTENDED_NOT_READY), test_extended_cancel);
  g_test_add_data_func("/focaltech-raw/bad-status-bcc", GINT_TO_POINTER(BAD_STATUS_BCC), test_failure);
  g_test_add_data_func("/focaltech-raw/bad-status-length", GINT_TO_POINTER(BAD_STATUS_LENGTH), test_failure);
  g_test_add_data_func("/focaltech-raw/unknown-status", GINT_TO_POINTER(UNKNOWN_STATUS), test_failure);
  g_test_add_data_func("/focaltech-raw/bad-frame-header", GINT_TO_POINTER(BAD_FRAME_HEADER), test_failure);
  g_test_add_data_func("/focaltech-raw/bad-frame-bcc", GINT_TO_POINTER(BAD_FRAME_BCC), test_failure);
  g_test_add_data_func("/focaltech-raw/bad-frame-length", GINT_TO_POINTER(BAD_FRAME_LENGTH), test_failure);
  g_test_add_data_func("/focaltech-raw/frame-rejected", GINT_TO_POINTER(FRAME_REJECTED), test_failure);
  g_test_add_data_func("/focaltech-raw/frame-empty-ack", GINT_TO_POINTER(FRAME_EMPTY_ACK), test_failure);
  g_test_add_data_func("/focaltech-raw/extended-without-trailer", GINT_TO_POINTER(EXTENDED_WITHOUT_TRAILER), test_response_delivery);
  g_test_add_data_func("/focaltech-raw/frame-without-trailer", GINT_TO_POINTER(FRAME_WITHOUT_TRAILER), test_failure);
  g_test_add_data_func("/focaltech-raw/extra-status-bytes", GINT_TO_POINTER(EXTRA_STATUS_BYTES), test_failure);
  g_test_add_data_func("/focaltech-raw/unknown-extended-status", GINT_TO_POINTER(UNKNOWN_EXTENDED_STATUS), test_failure);
  g_test_add_data_func("/focaltech-raw/extended-rejected", GINT_TO_POINTER(EXTENDED_REJECTED), test_failure);
  g_test_add_data_func("/focaltech-raw/bad-extended-frame", GINT_TO_POINTER(BAD_EXTENDED_FRAME), test_failure);
  g_test_add_data_func("/focaltech-raw/reply-deadline", GINT_TO_POINTER(REPLY_DEADLINE), test_failure);
  g_test_add_data_func("/focaltech-raw/coalesced-replies", GINT_TO_POINTER(COALESCED_REPLIES), test_failure);
  g_test_add_data_func("/focaltech-raw/recovery/initial-stale-frame", GINT_TO_POINTER(FALSE), test_initial_drain);
  g_test_add_data_func("/focaltech-raw/recovery/reopen-stale-frame", GINT_TO_POINTER(TRUE), test_initial_drain);
  g_test_add_data_func("/focaltech-raw/recovery/drain-limit", GINT_TO_POINTER(NORMAL), test_recovery_failure);
  g_test_add_data_func("/focaltech-raw/recovery/drain-partial-timeout", GINT_TO_POINTER(DRAIN_PARTIAL_TIMEOUT), test_recovery_failure);
  g_test_add_data_func("/focaltech-raw/recovery/late-stale-frame", GINT_TO_POINTER(STALE_HEALTH_REPLY), test_recovery_failure);
  g_test_add_data_func("/focaltech-raw/recovery/bad-bridge", GINT_TO_POINTER(BAD_BRIDGE_VALUE), test_recovery_failure);
  g_test_add_data_func("/focaltech-raw/recovery/bad-app", GINT_TO_POINTER(BAD_APP_VALUE), test_recovery_failure);
  g_test_add_data_func("/focaltech-raw/recovery/bad-dimension-a", GINT_TO_POINTER(BAD_DIMENSION_A), test_recovery_failure);
  g_test_add_data_func("/focaltech-raw/recovery/bad-dimension-b", GINT_TO_POINTER(BAD_DIMENSION_B), test_recovery_failure);
  g_test_add_data_func("/focaltech-raw/recovery/extended-zero", GINT_TO_POINTER(ZERO_EXTENDED_STATUS), test_recovery_failure);
  g_test_add_data_func("/focaltech-raw/cancellation/status-out", GINT_TO_POINTER(CANCEL_STATUS_OUT), test_cancel_boundary);
  g_test_add_data_func("/focaltech-raw/cancellation/frame-out", GINT_TO_POINTER(CANCEL_FRAME_OUT), test_cancel_boundary);
  g_test_add_data_func("/focaltech-raw/cancellation/with-io-error", GINT_TO_POINTER(CANCEL_AND_USB_ERROR), test_cancel_boundary);
  g_test_add_data_func("/focaltech-raw/cancellation/during-drain", GINT_TO_POINTER(CANCEL_PENDING), test_cancel_boundary);
  g_test_add_func("/focaltech-raw/ready-frame-without-contact", test_ready_frame_without_contact);
  g_test_add_data_func("/focaltech-raw/delivery/enroll-incompatible-frame", GINT_TO_POINTER(FPI_DEVICE_ACTION_ENROLL), test_frame_delivery_failure);
  g_test_add_data_func("/focaltech-raw/delivery/verify-incompatible-frame", GINT_TO_POINTER(FPI_DEVICE_ACTION_VERIFY), test_frame_delivery_failure);
  g_test_add_data_func("/focaltech-raw/delivery/identify-incompatible-frame", GINT_TO_POINTER(FPI_DEVICE_ACTION_IDENTIFY), test_frame_delivery_failure);
  g_test_add_data_func("/focaltech-raw/delivery/backend-error-keeps-synchronization", GINT_TO_POINTER(ALGORITHM_ERROR), test_error_recovery_boundary);
  g_test_add_data_func("/focaltech-raw/delivery/bad-bcc-requires-synchronization", GINT_TO_POINTER(BAD_FRAME_BCC), test_error_recovery_boundary);
  return g_test_run();
}
