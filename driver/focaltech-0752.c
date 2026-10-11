/*
 * FocalTech FT9362 (2808:0752) raw capture development driver
 * Copyright (C) 2025-2026 Ivan Kovalev
 * Copyright (C) 2026 HsingYun
 * SPDX-License-Identifier: LGPL-2.1-or-later
 */

#define FP_COMPONENT "focaltech0752"

#include <string.h>

#include "algorithm/entry.h"
#include "drivers_api.h"

enum {
  USB_VENDOR_ID = 0x2808,
  USB_PRODUCT_ID = 0x0752,
  USB_INTERFACE = 0,
  USB_ENDPOINT_IN = 0x81,
  USB_ENDPOINT_OUT = 0x03,
  USB_PACKET_SIZE = 64,
  IMAGE_DATA_SIZE = 12160,
  COMMAND_TIMEOUT_MS = 1000,
  FRAME_TIMEOUT_MS = 5000,
  DRAIN_TIMEOUT_MS = 200,
  DRAIN_BUDGET_MS = 1000,
  DRAIN_MAX_READS = 4,
  FINGER_POLL_INTERVAL_MS = 50
};

enum {
  REPLY_START_BYTE = 0x02,
  REPLY_SUCCESS = 0x04,
  REPLY_LENGTH_OFFSET = 1,
  REPLY_HEADER_SIZE = 3,
  REPLY_STATUS_OFFSET = REPLY_HEADER_SIZE,
  REPLY_STATUS_SIZE = 1,
  REPLY_DATA_OFFSET = REPLY_STATUS_OFFSET + REPLY_STATUS_SIZE,
  REPLY_BCC_SIZE = 1,
  REPLY_TRAILER_SIZE = 1,
  REPLY_MAX_SIZE = REPLY_DATA_OFFSET + IMAGE_DATA_SIZE + REPLY_BCC_SIZE + REPLY_TRAILER_SIZE,
  NORMAL_STATUS_NOT_READY = 0x00,
  NORMAL_STATUS_READY = 0x01,
  EXTENDED_STATUS_READY = 0x05,
  EXTENDED_STATUS_NOT_READY = 0x06
};

/* Wire request, response payload size, deadline, and optional health value. */
typedef struct {
  guint8 bytes[7];
  gsize size;
  gsize data_size;
  guint timeout_ms;
  guint8 expected[2];
  gboolean check_value;
} Ft9362Command;

static const Ft9362Command bridge_command = {{2, 0, 1, 0x34, 0x35}, 5, 2, COMMAND_TIMEOUT_MS, {0x55, 0xaa}, TRUE};
static const Ft9362Command app_command = {{2, 0, 3, 0x80, 1, 2, 0x80}, 7, 2, COMMAND_TIMEOUT_MS, {0xa5, 0x5a}, TRUE};
static const Ft9362Command dimension_a_command = {{2, 0, 3, 0x80, 3, 1, 0x81}, 7, 1, COMMAND_TIMEOUT_MS, {0x28}, TRUE};
static const Ft9362Command dimension_b_command = {{2, 0, 3, 0x80, 4, 1, 0x86}, 7, 1, COMMAND_TIMEOUT_MS, {0x98}, TRUE};
static const Ft9362Command finger_status_command = {{2, 0, 3, 0x80, 2, 1, 0x80}, 7, 1, COMMAND_TIMEOUT_MS};
static const Ft9362Command capture_command = {{2, 0, 1, 0x81, 0x80}, 5, IMAGE_DATA_SIZE, FRAME_TIMEOUT_MS};
static const Ft9362Command extended_status_command = {{2, 0, 3, 0x82, 0x73, 1, 0xf3}, 7, 1, COMMAND_TIMEOUT_MS};
static const Ft9362Command extended_capture_command = {{2, 0, 3, 0x82, 0x76, 1, 0xf6}, 7, IMAGE_DATA_SIZE, FRAME_TIMEOUT_MS};

static const FpIdEntry supported_devices[] = {
    {.vid = USB_VENDOR_ID, .pid = USB_PRODUCT_ID},
    {.vid = 0, .pid = 0, .driver_data = 0},
};

typedef enum {
  CAPTURE_SYNC_DRAIN,
  CAPTURE_SYNC_BRIDGE,
  CAPTURE_SYNC_APP,
  CAPTURE_SYNC_A,
  CAPTURE_SYNC_B,
  CAPTURE_CONTACT,
  CAPTURE_POLL,
  CAPTURE_REFRESH,
  CAPTURE_POLL_REFRESHED,
  CAPTURE_FRAME,
  CAPTURE_WAIT_POLL,
  CAPTURE_NUM_STATES
} Ft9362CapturePhase;

typedef struct {
  const Ft9362Command* command;
  guint8 reply[REPLY_MAX_SIZE];
  gsize bytes_received;
  gint64 reply_deadline_us;
  gboolean active;
  gboolean transfer_pending;
} Ft9362Transaction;

typedef struct {
  FpiSsm* machine;
  gint64 drain_deadline_us;
  guint drain_reads;
} Ft9362CaptureState;

struct _FpiDeviceFocaltech0752 {
  FpDevice parent_instance;
  Ft9362CaptureState capture;
  Ft9362Transaction transaction;
  /* Survives action cleanup: clearing host memory does not drain the device. */
  gboolean needs_sync;
};

G_DECLARE_FINAL_TYPE(FpiDeviceFocaltech0752, fpi_device_focaltech0752, FPI, DEVICE_FOCALTECH0752, FpDevice);
G_DEFINE_TYPE(FpiDeviceFocaltech0752, fpi_device_focaltech0752, FP_TYPE_DEVICE);

static void on_command_written(FpiUsbTransfer* transfer, FpDevice* dev, gpointer user_data, GError* error);
static void on_reply_received(FpiUsbTransfer* transfer, FpDevice* dev, gpointer user_data, GError* error);

static void transaction_clear(FpDevice* dev) {
  Ft9362Transaction* transaction = &FPI_DEVICE_FOCALTECH0752(dev)->transaction;
  g_assert(!transaction->active && !transaction->transfer_pending);
  memset(transaction, 0, sizeof(*transaction));
}

static void capture_cleanup(FpDevice* dev) {
  transaction_clear(dev);
  fpi_device_report_finger_status(dev, FP_FINGER_STATUS_NONE);
}

static void capture_fail(FpDevice* dev, GError* error) {
  capture_cleanup(dev);
  fpi_device_action_error(dev, error);
}

static gboolean capture_cancel_if_ready(FpDevice* dev) {
  Ft9362CaptureState* capture = &FPI_DEVICE_FOCALTECH0752(dev)->capture;
  if (capture->machine == NULL || FPI_DEVICE_FOCALTECH0752(dev)->transaction.active || !fpi_device_action_is_cancelled(dev)) {
    return FALSE;
  }

  fpi_ssm_mark_failed(capture->machine, g_error_new_literal(G_IO_ERROR, G_IO_ERROR_CANCELLED, "Raw capture cancelled"));
  return TRUE;
}

G_DEFINE_AUTO_CLEANUP_CLEAR_FUNC(Ft9362Template, ft9362_template_free)

static gboolean template_is_empty(const Ft9362Template* value) {
  return value->format == NULL && value->data == NULL && value->size == 0;
}

static gboolean template_is_valid(const Ft9362Template* value) {
  return value->format != NULL && g_utf8_validate(value->format, -1, NULL) && value->data != NULL && value->size != 0;
}

static GError* algorithm_status_error(Ft9362Status status) {
  if (status == FT9362_STATUS_OK) {
    return NULL;
  }

  FpDeviceError code = FP_DEVICE_ERROR_GENERAL;
  if (status == FT9362_STATUS_INVALID_TEMPLATE) {
    code = FP_DEVICE_ERROR_DATA_INVALID;
  } else if (status == FT9362_STATUS_NOT_IMPLEMENTED) {
    code = FP_DEVICE_ERROR_NOT_SUPPORTED;
  }
  return fpi_device_error_new_msg(code, "FT9362 processing failed (status %d)", status);
}

static GError* scan_result_error(Ft9362Status status, const Ft9362Template* scanned) {
  if (status == FT9362_STATUS_OK && !template_is_empty(scanned) && !template_is_valid(scanned)) {
    status = FT9362_STATUS_INTERNAL_ERROR;
  }
  return algorithm_status_error(status);
}

static void print_set_template(FpPrint* print, const Ft9362Template* value) {
  GVariant* bytes = g_variant_new_fixed_array(G_VARIANT_TYPE_BYTE, value->data, value->size, 1);
  fpi_print_set_type(print, FPI_PRINT_RAW);
  fpi_print_set_device_stored(print, FALSE);
  g_object_set(print, "fpi-data", g_variant_new("(s@ay)", value->format, bytes), NULL);
}

static FpPrint* print_from_template(FpDevice* dev, const Ft9362Template* value) {
  if (template_is_empty(value)) {
    return NULL;
  }

  FpPrint* print = g_object_ref_sink(fp_print_new(dev));
  print_set_template(print, value);
  return print;
}

static Ft9362TemplateView template_view_from_data(GVariant* data) {
  Ft9362TemplateView view = {0};
  g_autoptr(GVariant) bytes = NULL;
  gsize size = 0;

  if (data != NULL && g_variant_is_of_type(data, G_VARIANT_TYPE("(say)"))) {
    g_variant_get(data, "(&s@ay)", &view.format, &bytes);
    view.data = g_variant_get_fixed_array(bytes, &size, 1);
    view.size = size;
  }
  return view;
}

static void process_enrollment(FpDevice* dev, const Ft9362Frame* frame) {
  g_auto(Ft9362Template) output = {0};
  Ft9362Status status = ft9362_enroll(frame, &output);
  if (status == FT9362_STATUS_OK && !template_is_valid(&output)) {
    status = FT9362_STATUS_INTERNAL_ERROR;
  }
  GError* error = algorithm_status_error(status);
  if (error != NULL) {
    capture_fail(dev, error);
    return;
  }

  FpPrint* enrolled;
  fpi_device_get_enroll_data(dev, &enrolled);
  g_autoptr(FpPrint) print = g_object_ref(enrolled);
  print_set_template(print, &output);
  capture_cleanup(dev);
  fpi_device_enroll_progress(dev, 1, NULL, NULL);
  fpi_device_enroll_complete(dev, g_steal_pointer(&print), NULL);
}

static void process_verification(FpDevice* dev, const Ft9362Frame* frame) {
  FpPrint* reference;
  g_autoptr(GVariant) reference_data = NULL;
  Ft9362VerifyResult output = {0};
  fpi_device_get_verify_data(dev, &reference);
  g_object_get(reference, "fpi-data", &reference_data, NULL);
  Ft9362TemplateView view = template_view_from_data(reference_data);
  Ft9362Status status = ft9362_verify(frame, &view, &output);
  g_auto(Ft9362Template) scanned = output.scanned;
  GError* error = scan_result_error(status, &scanned);
  if (error != NULL) {
    capture_fail(dev, error);
    return;
  }

  g_autoptr(FpPrint) print = print_from_template(dev, &scanned);
  if (output.matched && print != NULL && (fpi_print_get_type(reference) != FPI_PRINT_RAW || reference_data == NULL || !fp_print_equal(print, reference))) {
    g_clear_object(&print);
  }
  capture_cleanup(dev);
  fpi_device_verify_report(dev, output.matched ? FPI_MATCH_SUCCESS : FPI_MATCH_FAIL, print, NULL);
  fpi_device_verify_complete(dev, NULL);
}

static void process_identification(FpDevice* dev, const Ft9362Frame* frame) {
  GPtrArray* prints;
  Ft9362IdentifyResult output = {.matched_index = FT9362_NO_MATCH};
  fpi_device_get_identify_data(dev, &prints);
  g_autofree Ft9362TemplateView* gallery = g_new0(Ft9362TemplateView, prints->len);
  g_autoptr(GPtrArray) gallery_data = g_ptr_array_new_with_free_func((GDestroyNotify)g_variant_unref);
  for (guint i = 0; i < prints->len; i++) {
    GVariant* data = NULL;
    g_object_get(g_ptr_array_index(prints, i), "fpi-data", &data, NULL);
    gallery[i] = template_view_from_data(data);
    if (data != NULL) {
      g_ptr_array_add(gallery_data, data);
    }
  }

  Ft9362Status status = ft9362_identify(frame, gallery, prints->len, &output);
  g_auto(Ft9362Template) scanned = output.scanned;
  if (status == FT9362_STATUS_OK && output.matched_index != FT9362_NO_MATCH && output.matched_index >= prints->len) {
    status = FT9362_STATUS_INTERNAL_ERROR;
  }
  GError* error = scan_result_error(status, &scanned);
  if (error != NULL) {
    capture_fail(dev, error);
    return;
  }

  FpPrint* match = output.matched_index == FT9362_NO_MATCH ? NULL : g_ptr_array_index(prints, output.matched_index);
  g_autoptr(FpPrint) print = print_from_template(dev, &scanned);
  if (match != NULL && print != NULL && (fpi_print_get_type(match) != FPI_PRINT_RAW || gallery[output.matched_index].data == NULL || !fp_print_equal(print, match))) {
    g_clear_object(&print);
  }
  capture_cleanup(dev);
  fpi_device_identify_report(dev, match, print, NULL);
  fpi_device_identify_complete(dev, NULL);
}

static void capture_process_frame(FpDevice* dev) {
  Ft9362Transaction* transaction = &FPI_DEVICE_FOCALTECH0752(dev)->transaction;
  /* The wire transaction has already completed. Representation/backend errors
   * must not change device synchronization or cause a drain on the next action. */
  if (transaction->bytes_received != FT9362_RAW_FRAME_SIZE) {
    capture_fail(dev, fpi_device_error_new_msg(FP_DEVICE_ERROR_NOT_SUPPORTED, "Image response is incompatible with FT9362-RAW-v1"));
    return;
  }
  Ft9362Frame frame = {transaction->reply, transaction->bytes_received};
  FpiDeviceAction action = fpi_device_get_current_action(dev);
  if (action == FPI_DEVICE_ACTION_ENROLL) {
    process_enrollment(dev, &frame);
  } else if (action == FPI_DEVICE_ACTION_VERIFY) {
    process_verification(dev, &frame);
  } else if (action == FPI_DEVICE_ACTION_IDENTIFY) {
    process_identification(dev, &frame);
  } else {
    capture_fail(dev, fpi_device_error_new(FP_DEVICE_ERROR_GENERAL));
  }
}

/* Transport owns OUT + the entire reply, independently of action cancellation. */
static void transaction_fail(FpDevice* dev, GError* error) {
  FpiDeviceFocaltech0752* self = FPI_DEVICE_FOCALTECH0752(dev);
  self->needs_sync = TRUE;
  self->transaction.active = FALSE;
  if (capture_cancel_if_ready(dev)) {
    g_error_free(error);
    return;
  }
  fpi_ssm_mark_failed(self->capture.machine, error);
}

static void usb_submit(FpiUsbTransfer* transfer, guint timeout_ms, FpiUsbTransferCallback callback) {
  FpiDeviceFocaltech0752* self = FPI_DEVICE_FOCALTECH0752(transfer->device);
  g_assert(!self->transaction.transfer_pending);
  transfer->ssm = self->capture.machine;
  self->transaction.transfer_pending = TRUE;
  fpi_usb_transfer_submit(transfer, timeout_ms, NULL, callback, NULL);
}

static void usb_read_reply(FpDevice* dev) {
  Ft9362Transaction* transaction = &FPI_DEVICE_FOCALTECH0752(dev)->transaction;
  gint64 remaining_us = transaction->reply_deadline_us - g_get_monotonic_time();
  if (remaining_us <= 0) {
    transaction_fail(dev, g_error_new_literal(G_USB_DEVICE_ERROR, G_USB_DEVICE_ERROR_TIMED_OUT, "USB response deadline expired"));
    return;
  }

  /* Even a status request may encounter a stale image. Never truncate it to 64 bytes. */
  gsize capacity = ((sizeof(transaction->reply) - transaction->bytes_received + USB_PACKET_SIZE - 1) / USB_PACKET_SIZE) * USB_PACKET_SIZE;
  FpiUsbTransfer* transfer = fpi_usb_transfer_new(dev);
  fpi_usb_transfer_fill_bulk(transfer, USB_ENDPOINT_IN, capacity);
  usb_submit(transfer, (remaining_us + G_TIME_SPAN_MILLISECOND - 1) / G_TIME_SPAN_MILLISECOND, on_reply_received);
}

static void transaction_start(FpDevice* dev, const Ft9362Command* command) {
  Ft9362Transaction* transaction = &FPI_DEVICE_FOCALTECH0752(dev)->transaction;
  transaction_clear(dev);
  transaction->command = command;
  transaction->active = TRUE;
  FpiUsbTransfer* transfer = fpi_usb_transfer_new(dev);
  fpi_usb_transfer_fill_bulk_full(transfer, USB_ENDPOINT_OUT, (guint8*)command->bytes, command->size, NULL);
  transfer->short_is_error = TRUE;
  usb_submit(transfer, COMMAND_TIMEOUT_MS, on_command_written);
}

static gboolean transaction_transfer_finished(FpiUsbTransfer* transfer, GError* error) {
  FpDevice* dev = transfer->device;
  FPI_DEVICE_FOCALTECH0752(dev)->transaction.transfer_pending = FALSE;
  if (error != NULL) {
    transaction_fail(dev, error);
    return FALSE;
  }
  if (transfer->endpoint == USB_ENDPOINT_OUT && transfer->actual_length != transfer->length) {
    transaction_fail(dev, fpi_device_error_new_msg(FP_DEVICE_ERROR_PROTO, "Incomplete USB command write"));
    return FALSE;
  }
  return TRUE;
}

static void on_command_written(FpiUsbTransfer* transfer, FpDevice* dev, gpointer user_data, GError* error) {
  if (!transaction_transfer_finished(transfer, error)) {
    return;
  }
  Ft9362Transaction* transaction = &FPI_DEVICE_FOCALTECH0752(dev)->transaction;
  transaction->reply_deadline_us = g_get_monotonic_time() + transaction->command->timeout_ms * G_TIME_SPAN_MILLISECOND;
  usb_read_reply(dev);
}

static GError* reply_validate(const guint8* reply, gsize size, gsize data_size) {
  gsize content_size = ((gsize)reply[REPLY_LENGTH_OFFSET] << 8) | reply[REPLY_LENGTH_OFFSET + 1];
  gsize logical_size = content_size + REPLY_HEADER_SIZE + REPLY_BCC_SIZE;
  if (size > logical_size + REPLY_TRAILER_SIZE) {
    return fpi_device_error_new_msg(FP_DEVICE_ERROR_PROTO, "Unexpected bytes after USB response");
  }
  guint8 checksum = 0;
  for (gsize i = REPLY_LENGTH_OFFSET; i < logical_size; i++) {
    checksum ^= reply[i];
  }
  if (checksum != 0) {
    return fpi_device_error_new_msg(FP_DEVICE_ERROR_PROTO, "USB response BCC mismatch");
  }
  if (reply[REPLY_STATUS_OFFSET] != REPLY_SUCCESS) {
    return fpi_device_error_new_msg(FP_DEVICE_ERROR_PROTO, "Device rejected command (status 0x%02x)", reply[REPLY_STATUS_OFFSET]);
  }
  if (content_size != data_size + REPLY_STATUS_SIZE) {
    return fpi_device_error_new_msg(FP_DEVICE_ERROR_PROTO, "Unexpected USB response data length: %zu", content_size - REPLY_STATUS_SIZE);
  }
  return NULL;
}

/* Acquisition sees only validated, complete transactions. */
static void capture_transaction_done(FpDevice* dev) {
  FpiDeviceFocaltech0752* self = FPI_DEVICE_FOCALTECH0752(dev);
  FpiSsm* machine = self->capture.machine;
  Ft9362CapturePhase phase = fpi_ssm_get_cur_state(machine);
  guint8 value = self->transaction.reply[REPLY_DATA_OFFSET];
  /* Validate semantic state even when cancellation races the final callback. */
  if ((phase == CAPTURE_CONTACT && value != EXTENDED_STATUS_READY && value != EXTENDED_STATUS_NOT_READY) || ((phase == CAPTURE_POLL || phase == CAPTURE_POLL_REFRESHED) && value != NORMAL_STATUS_READY && value != NORMAL_STATUS_NOT_READY)) {
    transaction_fail(dev, fpi_device_error_new_msg(FP_DEVICE_ERROR_PROTO, "Unknown capture status 0x%02x in phase %d", value, phase));
    return;
  }
  if (capture_cancel_if_ready(dev)) {
    return;
  }
  switch (phase) {
    case CAPTURE_SYNC_BRIDGE:
    case CAPTURE_SYNC_APP:
    case CAPTURE_SYNC_A:
    case CAPTURE_SYNC_B:
      fpi_ssm_next_state(machine);
      break;
    case CAPTURE_CONTACT:
      self->needs_sync = FALSE;
      fpi_device_report_finger_status(dev, value == EXTENDED_STATUS_READY ? FP_FINGER_STATUS_PRESENT : FP_FINGER_STATUS_NEEDED);
      fpi_ssm_jump_to_state(machine, value == EXTENDED_STATUS_READY ? CAPTURE_POLL : CAPTURE_WAIT_POLL);
      break;
    case CAPTURE_POLL:
    case CAPTURE_POLL_REFRESHED:
      if (value == NORMAL_STATUS_READY) {
        fpi_ssm_jump_to_state(machine, CAPTURE_FRAME);
      } else {
        fpi_ssm_jump_to_state(machine, phase == CAPTURE_POLL ? CAPTURE_REFRESH : CAPTURE_WAIT_POLL);
      }
      break;
    case CAPTURE_REFRESH:
      fpi_ssm_next_state(machine);
      break;
    case CAPTURE_FRAME:
      fpi_ssm_mark_completed(machine);
      break;
    case CAPTURE_SYNC_DRAIN:
    case CAPTURE_WAIT_POLL:
    case CAPTURE_NUM_STATES:
    default:
      g_assert_not_reached();
  }
}

static void on_reply_received(FpiUsbTransfer* transfer, FpDevice* dev, gpointer user_data, GError* error) {
  Ft9362Transaction* transaction = &FPI_DEVICE_FOCALTECH0752(dev)->transaction;
  if (!transaction_transfer_finished(transfer, error)) {
    return;
  }
  if (transfer->actual_length <= 0 || transfer->actual_length > transfer->length || (gsize)transfer->actual_length > sizeof(transaction->reply) - transaction->bytes_received) {
    transaction_fail(dev, fpi_device_error_new_msg(FP_DEVICE_ERROR_PROTO, "Invalid USB response chunk length"));
    return;
  }
  memcpy(transaction->reply + transaction->bytes_received, transfer->buffer, transfer->actual_length);
  transaction->bytes_received += transfer->actual_length;
  if (transaction->reply[0] != REPLY_START_BYTE) {
    transaction_fail(dev, fpi_device_error_new_msg(FP_DEVICE_ERROR_PROTO, "Invalid USB response header"));
    return;
  }
  if (transaction->bytes_received < REPLY_HEADER_SIZE) {
    usb_read_reply(dev);
    return;
  }
  gsize content_size = ((gsize)transaction->reply[REPLY_LENGTH_OFFSET] << 8) | transaction->reply[REPLY_LENGTH_OFFSET + 1];
  if (content_size < REPLY_STATUS_SIZE || content_size > IMAGE_DATA_SIZE + REPLY_STATUS_SIZE) {
    transaction_fail(dev, fpi_device_error_new_msg(FP_DEVICE_ERROR_PROTO, "Invalid USB response content length: %zu", content_size));
    return;
  }
  if (transaction->bytes_received < content_size + REPLY_HEADER_SIZE + REPLY_BCC_SIZE) {
    usb_read_reply(dev);
    return;
  }
  const Ft9362Command* command = transaction->command;
  error = reply_validate(transaction->reply, transaction->bytes_received, command->data_size);
  if (error == NULL && command->check_value && memcmp(transaction->reply + REPLY_DATA_OFFSET, command->expected, command->data_size) != 0) {
    error = fpi_device_error_new_msg(FP_DEVICE_ERROR_PROTO, "Device health response value mismatch");
  }
  if (error != NULL) {
    transaction_fail(dev, error);
    return;
  }
  transaction->active = FALSE;
  capture_transaction_done(dev);
}

static void sync_drain(FpDevice* dev);

static void on_drain_received(FpiUsbTransfer* transfer, FpDevice* dev, gpointer user_data, GError* error) {
  FpiDeviceFocaltech0752* self = FPI_DEVICE_FOCALTECH0752(dev);
  self->transaction.transfer_pending = FALSE;
  self->transaction.active = FALSE;
  /* GUsb returns -1 on timeout and hides libusb's partial length. This is
   * only the end of the drain window, not proof of an empty device queue.
   * The following full-capacity health transactions must still succeed. */
  gboolean timed_out = g_error_matches(error, G_USB_DEVICE_ERROR, G_USB_DEVICE_ERROR_TIMED_OUT);
  if (capture_cancel_if_ready(dev)) {
    g_clear_error(&error);
    return;
  }
  if (timed_out) {
    g_clear_error(&error);
    fpi_ssm_next_state(self->capture.machine);
  } else if (error != NULL) {
    transaction_fail(dev, error);
  } else if (transfer->actual_length <= 0 || transfer->actual_length > transfer->length) {
    transaction_fail(dev, fpi_device_error_new_msg(FP_DEVICE_ERROR_PROTO, "Invalid drain response length"));
  } else {
    sync_drain(dev);
  }
}

static void sync_drain(FpDevice* dev) {
  FpiDeviceFocaltech0752* self = FPI_DEVICE_FOCALTECH0752(dev);
  gint64 remaining_us = self->capture.drain_deadline_us - g_get_monotonic_time();
  if (self->capture.drain_reads >= DRAIN_MAX_READS || remaining_us <= 0) {
    transaction_fail(dev, fpi_device_error_new_msg(FP_DEVICE_ERROR_PROTO, "USB input did not become quiet within the drain budget"));
    return;
  }
  self->capture.drain_reads++;
  self->transaction.active = TRUE;
  FpiUsbTransfer* transfer = fpi_usb_transfer_new(dev);
  fpi_usb_transfer_fill_bulk(transfer, USB_ENDPOINT_IN, ((REPLY_MAX_SIZE + USB_PACKET_SIZE - 1) / USB_PACKET_SIZE) * USB_PACKET_SIZE);
  usb_submit(transfer, MIN(DRAIN_TIMEOUT_MS, (remaining_us + G_TIME_SPAN_MILLISECOND - 1) / G_TIME_SPAN_MILLISECOND), on_drain_received);
}

static void capture_run_state(FpiSsm* machine, FpDevice* dev) {
  FpiDeviceFocaltech0752* self = FPI_DEVICE_FOCALTECH0752(dev);
  if (capture_cancel_if_ready(dev)) {
    return;
  }
  switch ((Ft9362CapturePhase)fpi_ssm_get_cur_state(machine)) {
    case CAPTURE_SYNC_DRAIN:
      if (!self->needs_sync) {
        fpi_ssm_jump_to_state(machine, CAPTURE_CONTACT);
        break;
      }
      self->capture.drain_reads = 0;
      self->capture.drain_deadline_us = g_get_monotonic_time() + DRAIN_BUDGET_MS * G_TIME_SPAN_MILLISECOND;
      sync_drain(dev);
      break;
    case CAPTURE_SYNC_BRIDGE:
      transaction_start(dev, &bridge_command);
      break;
    case CAPTURE_SYNC_APP:
      transaction_start(dev, &app_command);
      break;
    case CAPTURE_SYNC_A:
      transaction_start(dev, &dimension_a_command);
      break;
    case CAPTURE_SYNC_B:
      transaction_start(dev, &dimension_b_command);
      break;
    case CAPTURE_CONTACT:
      transaction_start(dev, &extended_status_command);
      break;
    case CAPTURE_POLL:
    case CAPTURE_POLL_REFRESHED:
      transaction_start(dev, &finger_status_command);
      break;
    case CAPTURE_REFRESH:
      transaction_start(dev, &extended_capture_command);
      break;
    case CAPTURE_FRAME:
      transaction_start(dev, &capture_command);
      break;
    case CAPTURE_WAIT_POLL:
      fpi_ssm_jump_to_state_delayed(machine, CAPTURE_CONTACT, FINGER_POLL_INTERVAL_MS);
      break;
    case CAPTURE_NUM_STATES:
    default:
      g_assert_not_reached();
  }
}

static void capture_done(FpiSsm* machine, FpDevice* dev, GError* error) {
  FPI_DEVICE_FOCALTECH0752(dev)->capture.machine = NULL;
  if (error != NULL) {
    capture_fail(dev, error);
    return;
  }
  capture_process_frame(dev);
}

static void device_open(FpDevice* dev) {
  FPI_DEVICE_FOCALTECH0752(dev)->needs_sync = TRUE;
  GError* error = NULL;
  GUsbDevice* usb = fpi_device_get_usb_device(dev);
  if (!g_usb_device_claim_interface(usb, USB_INTERFACE, 0, &error)) {
    fpi_device_open_complete(dev, error);
    return;
  }
  fpi_device_open_complete(dev, NULL);
}

static void device_close(FpDevice* dev) {
  GError* error = NULL;
  GUsbDevice* usb = fpi_device_get_usb_device(dev);
  transaction_clear(dev);
  if (!g_usb_device_release_interface(usb, USB_INTERFACE, 0, &error)) {
    fpi_device_close_complete(dev, error);
    return;
  }
  fpi_device_close_complete(dev, NULL);
}

static void device_start_capture(FpDevice* dev) {
  Ft9362CaptureState* capture = &FPI_DEVICE_FOCALTECH0752(dev)->capture;
  transaction_clear(dev);
  fpi_device_report_finger_status(dev, FP_FINGER_STATUS_NEEDED);
  capture->machine = fpi_ssm_new(dev, capture_run_state, CAPTURE_NUM_STATES);
  fpi_ssm_start(capture->machine, capture_done);
}

static void device_cancel(FpDevice* dev) {
  Ft9362CaptureState* capture = &FPI_DEVICE_FOCALTECH0752(dev)->capture;
  if (capture->machine != NULL && !FPI_DEVICE_FOCALTECH0752(dev)->transaction.active && fpi_ssm_get_cur_state(capture->machine) == CAPTURE_WAIT_POLL) {
    fpi_ssm_cancel_delayed_state_change(capture->machine);
  }
  capture_cancel_if_ready(dev);
}

static void device_finalize(GObject* object) {
  transaction_clear(FP_DEVICE(object));
  G_OBJECT_CLASS(fpi_device_focaltech0752_parent_class)->finalize(object);
}

static void fpi_device_focaltech0752_init(FpiDeviceFocaltech0752* self) {}

static void fpi_device_focaltech0752_class_init(FpiDeviceFocaltech0752Class* klass) {
  FpDeviceClass* device_class = FP_DEVICE_CLASS(klass);
  G_OBJECT_CLASS(klass)->finalize = device_finalize;
  device_class->id = FP_COMPONENT;
  device_class->full_name = "FocalTech FT9362";
  device_class->type = FP_DEVICE_TYPE_USB;
  device_class->id_table = supported_devices;
  device_class->scan_type = FP_SCAN_TYPE_PRESS;
  device_class->nr_enroll_stages = 1;
  device_class->features = FP_DEVICE_FEATURE_IDENTIFY | FP_DEVICE_FEATURE_VERIFY;
  device_class->open = device_open;
  device_class->close = device_close;
  device_class->enroll = device_start_capture;
  device_class->verify = device_start_capture;
  device_class->identify = device_start_capture;
  device_class->cancel = device_cancel;
}
