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
  STATUS_REPLY_CAPACITY = 64,
  STATUS_REPLY_MIN_SIZE = 7,
  COMMAND_TIMEOUT_MS = 1000,
  FRAME_TIMEOUT_MS = 5000,
  FINGER_POLL_INTERVAL_MS = 50
};

static const guint8 finger_status_command[] = {0x02, 0x00, 0x03, 0x80, 0x02, 0x01, 0x80};
static const guint8 capture_command[] = {0x02, 0x00, 0x01, 0x81, 0x80};

static const FpIdEntry supported_devices[] = {
    {.vid = USB_VENDOR_ID, .pid = USB_PRODUCT_ID},
    {.vid = 0, .pid = 0, .driver_data = 0},
};

typedef enum {
  CAPTURE_SEND_POLL,
  CAPTURE_READ_STATUS,
  CAPTURE_WAIT_POLL,
  CAPTURE_SEND_COMMAND,
  CAPTURE_READ_FRAME,
  CAPTURE_NUM_STATES
} Ft9362CapturePhase;

typedef struct {
  guint8 frame[FT9362_RAW_FRAME_SIZE];
  gsize bytes_received;
  FpiSsm* machine;
  gboolean transfer_pending;
} Ft9362CaptureState;

struct _FpiDeviceFocaltech0752 {
  FpDevice parent_instance;
  Ft9362CaptureState capture;
};

G_DECLARE_FINAL_TYPE(FpiDeviceFocaltech0752, fpi_device_focaltech0752, FPI, DEVICE_FOCALTECH0752, FpDevice);
G_DEFINE_TYPE(FpiDeviceFocaltech0752, fpi_device_focaltech0752, FP_TYPE_DEVICE);

static void on_command_written(FpiUsbTransfer* transfer, FpDevice* dev, gpointer user_data, GError* error);
static void on_finger_status_received(FpiUsbTransfer* transfer, FpDevice* dev, gpointer user_data, GError* error);
static void on_frame_received(FpiUsbTransfer* transfer, FpDevice* dev, gpointer user_data, GError* error);

static void capture_reset(FpDevice* dev) {
  Ft9362CaptureState* capture = &FPI_DEVICE_FOCALTECH0752(dev)->capture;
  memset(capture->frame, 0, sizeof(capture->frame));
  capture->bytes_received = 0;
}

static void capture_cleanup(FpDevice* dev) {
  capture_reset(dev);
  fpi_device_report_finger_status(dev, FP_FINGER_STATUS_NONE);
}

static void capture_fail(FpDevice* dev, GError* error) {
  capture_cleanup(dev);
  fpi_device_action_error(dev, error);
}

static gboolean capture_cancel_if_ready(FpDevice* dev) {
  Ft9362CaptureState* capture = &FPI_DEVICE_FOCALTECH0752(dev)->capture;
  if (capture->machine == NULL || capture->transfer_pending || !fpi_device_action_is_cancelled(dev)) {
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
  capture_cleanup(dev);
  fpi_device_identify_report(dev, match, print, NULL);
  fpi_device_identify_complete(dev, NULL);
}

static void capture_process_frame(FpDevice* dev) {
  Ft9362CaptureState* capture = &FPI_DEVICE_FOCALTECH0752(dev)->capture;
  Ft9362Frame frame = {capture->frame, capture->bytes_received};
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

static void usb_submit(FpiUsbTransfer* transfer, guint timeout_ms, FpiUsbTransferCallback callback) {
  FpDevice* dev = transfer->device;
  Ft9362CaptureState* capture = &FPI_DEVICE_FOCALTECH0752(dev)->capture;
  transfer->ssm = capture->machine;
  capture->transfer_pending = TRUE;
  fpi_usb_transfer_submit(transfer, timeout_ms, fpi_device_get_cancellable(dev), callback, NULL);
}

static void usb_write_command(FpDevice* dev, const guint8* command, gsize size, FpiUsbTransferCallback callback) {
  FpiUsbTransfer* transfer = fpi_usb_transfer_new(dev);
  fpi_usb_transfer_fill_bulk_full(transfer, USB_ENDPOINT_OUT, (guint8*)command, size, NULL);
  transfer->short_is_error = TRUE;
  usb_submit(transfer, COMMAND_TIMEOUT_MS, callback);
}

static void usb_read_reply(FpDevice* dev, gsize capacity, guint timeout_ms, FpiUsbTransferCallback callback) {
  FpiUsbTransfer* transfer = fpi_usb_transfer_new(dev);
  fpi_usb_transfer_fill_bulk(transfer, USB_ENDPOINT_IN, capacity);
  usb_submit(transfer, timeout_ms, callback);
}

static gboolean capture_handle_transfer_result(FpiUsbTransfer* transfer, GError* error) {
  FpDevice* dev = transfer->device;
  FPI_DEVICE_FOCALTECH0752(dev)->capture.transfer_pending = FALSE;
  if (capture_cancel_if_ready(dev)) {
    g_clear_error(&error);
    return FALSE;
  }
  if (error != NULL) {
    fpi_ssm_mark_failed(transfer->ssm, error);
    return FALSE;
  }
  if (transfer->endpoint == USB_ENDPOINT_OUT && transfer->actual_length != transfer->length) {
    fpi_ssm_mark_failed(transfer->ssm, fpi_device_error_new_msg(FP_DEVICE_ERROR_PROTO, "Incomplete USB command write"));
    return FALSE;
  }
  return TRUE;
}

static void capture_run_state(FpiSsm* machine, FpDevice* dev) {
  if (capture_cancel_if_ready(dev)) {
    return;
  }
  switch ((Ft9362CapturePhase)fpi_ssm_get_cur_state(machine)) {
    case CAPTURE_SEND_POLL:
      usb_write_command(dev, finger_status_command, sizeof(finger_status_command), on_command_written);
      break;
    case CAPTURE_READ_STATUS:
      usb_read_reply(dev, STATUS_REPLY_CAPACITY, COMMAND_TIMEOUT_MS, on_finger_status_received);
      break;
    case CAPTURE_WAIT_POLL:
      fpi_ssm_jump_to_state_delayed(machine, CAPTURE_SEND_POLL, FINGER_POLL_INTERVAL_MS);
      break;
    case CAPTURE_SEND_COMMAND:
      usb_write_command(dev, capture_command, sizeof(capture_command), on_command_written);
      break;
    case CAPTURE_READ_FRAME:
      usb_read_reply(dev, FT9362_RAW_FRAME_SIZE, FRAME_TIMEOUT_MS, on_frame_received);
      break;
    case CAPTURE_NUM_STATES:
    default:
      g_assert_not_reached();
  }
}

static void on_command_written(FpiUsbTransfer* transfer, FpDevice* dev, gpointer user_data, GError* error) {
  if (!capture_handle_transfer_result(transfer, error)) {
    return;
  }
  fpi_ssm_next_state(transfer->ssm);
}

static void on_finger_status_received(FpiUsbTransfer* transfer, FpDevice* dev, gpointer user_data, GError* error) {
  if (!capture_handle_transfer_result(transfer, error)) {
    return;
  }
  if (transfer->actual_length < STATUS_REPLY_MIN_SIZE || transfer->actual_length > transfer->length) {
    fpi_ssm_mark_failed(transfer->ssm, fpi_device_error_new_msg(FP_DEVICE_ERROR_PROTO, "Invalid status response length"));
    return;
  }
  if (transfer->buffer[0] != 0x02 || transfer->buffer[3] != 0x04) {
    fpi_ssm_mark_failed(transfer->ssm, fpi_device_error_new_msg(FP_DEVICE_ERROR_PROTO, "Unexpected status response header or type"));
    return;
  }

  if (transfer->buffer[4] == 0x01) {
    fpi_device_report_finger_status(dev, FP_FINGER_STATUS_PRESENT);
    fpi_ssm_jump_to_state(transfer->ssm, CAPTURE_SEND_COMMAND);
    return;
  }
  fpi_ssm_jump_to_state(transfer->ssm, CAPTURE_WAIT_POLL);
}

static void on_frame_received(FpiUsbTransfer* transfer, FpDevice* dev, gpointer user_data, GError* error) {
  Ft9362CaptureState* capture = &FPI_DEVICE_FOCALTECH0752(dev)->capture;
  if (!capture_handle_transfer_result(transfer, error)) {
    return;
  }

  gsize remaining = sizeof(capture->frame) - capture->bytes_received;
  if (transfer->actual_length <= 0 || (gsize)transfer->actual_length > remaining) {
    fpi_ssm_mark_failed(transfer->ssm, fpi_device_error_new_msg(FP_DEVICE_ERROR_PROTO, "Invalid raw frame chunk length"));
    return;
  }
  memcpy(capture->frame + capture->bytes_received, transfer->buffer, transfer->actual_length);
  capture->bytes_received += transfer->actual_length;
  if (capture->bytes_received == sizeof(capture->frame)) {
    fpi_ssm_mark_completed(transfer->ssm);
    return;
  }
  fpi_ssm_jump_to_state(transfer->ssm, CAPTURE_READ_FRAME);
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
  capture_reset(dev);
  if (!g_usb_device_release_interface(usb, USB_INTERFACE, 0, &error)) {
    fpi_device_close_complete(dev, error);
    return;
  }
  fpi_device_close_complete(dev, NULL);
}

static void device_start_capture(FpDevice* dev) {
  Ft9362CaptureState* capture = &FPI_DEVICE_FOCALTECH0752(dev)->capture;
  capture_reset(dev);
  fpi_device_report_finger_status(dev, FP_FINGER_STATUS_NEEDED);
  capture->machine = fpi_ssm_new(dev, capture_run_state, CAPTURE_NUM_STATES);
  fpi_ssm_start(capture->machine, capture_done);
}

static void device_cancel(FpDevice* dev) {
  Ft9362CaptureState* capture = &FPI_DEVICE_FOCALTECH0752(dev)->capture;
  if (capture->machine != NULL && !capture->transfer_pending && fpi_ssm_get_cur_state(capture->machine) == CAPTURE_WAIT_POLL) {
    fpi_ssm_cancel_delayed_state_change(capture->machine);
  }
  capture_cancel_if_ready(dev);
}

static void device_finalize(GObject* object) {
  capture_reset(FP_DEVICE(object));
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
