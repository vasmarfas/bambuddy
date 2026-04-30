"""Action dispatch for Obico failure detection.

Separated from the detection loop so actions can be unit-tested and swapped.
"""

import logging
from io import BytesIO

from PIL import Image, ImageDraw

from sqlalchemy import select

from backend.app.core.database import async_session
from backend.app.models.printer import Printer

logger = logging.getLogger(__name__)


async def execute_action(
    printer_id: int,
    action: str,
    task_name: str,
    score: float,
    frame: bytes | None = None,
    detections: list | None = None,
) -> None:
    """Run the configured action for a detected print failure.

    action: 'notify' | 'pause' | 'pause_and_off'
    """
    printer_name = await _get_printer_name(printer_id)

    if action in ("pause", "pause_and_off"):
        _pause_print(printer_id)

    if action == "pause_and_off":
        await _turn_off_linked_plugs(printer_id)

    await _notify(
        printer_id=printer_id,
        printer_name=printer_name,
        task_name=task_name,
        score=score,
        action=action,
        frame=frame,
        detections=detections,
    )


async def _get_printer_name(printer_id: int) -> str:
    async with async_session() as db:
        result = await db.execute(select(Printer).where(Printer.id == printer_id))
        printer = result.scalar_one_or_none()
    return printer.name if printer else f"Printer {printer_id}"


def _pause_print(printer_id: int) -> None:
    from backend.app.services.printer_manager import printer_manager

    client = printer_manager.get_client(printer_id)
    if not client:
        logger.warning("Obico pause: no MQTT client for printer %s", printer_id)
        return
    if not client.pause_print():
        logger.warning("Obico pause: pause_print() returned False for printer %s", printer_id)


async def _turn_off_linked_plugs(printer_id: int) -> None:
    from backend.app.services.smart_plug_manager import smart_plug_manager

    async with async_session() as db:
        plugs = await smart_plug_manager._get_plugs_for_printer(printer_id, db)
        for plug in plugs:
            if not plug.enabled:
                continue
            try:
                service = await smart_plug_manager.get_service_for_plug(plug, db)
                await service.turn_off(plug)
                logger.info("Obico action: turned off plug %s for printer %s", plug.name, printer_id)
            except Exception as e:
                logger.error("Obico action: failed to turn off plug %s: %s", plug.name, e)


def _draw_detection_boxes(frame: bytes | None, detections: list | None) -> bytes | None:
    """Draw Obico detection boxes on frame when possible.

    Supports boxes as normalized coordinates (0..1) or absolute pixel values.
    Expected item shape: [label, confidence, [x1, y1, x2, y2]].
    """
    if not frame:
        return None
    if not detections:
        return frame

    try:
        image = Image.open(BytesIO(frame)).convert("RGB")
        draw = ImageDraw.Draw(image)
        width, height = image.size

        for det in detections:
            if not isinstance(det, (list, tuple)) or len(det) < 3:
                continue
            bbox = det[2]
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                continue

            x1, y1, x2, y2 = bbox
            if all(isinstance(v, (int, float)) for v in (x1, y1, x2, y2)):
                # Many detectors return normalized coordinates.
                if 0.0 <= x1 <= 1.0 and 0.0 <= y1 <= 1.0 and 0.0 <= x2 <= 1.0 and 0.0 <= y2 <= 1.0:
                    x1, x2 = x1 * width, x2 * width
                    y1, y2 = y1 * height, y2 * height

                draw.rectangle([(x1, y1), (x2, y2)], outline=(255, 64, 64), width=4)

        output = BytesIO()
        image.save(output, format="JPEG", quality=90)
        return output.getvalue()
    except Exception:
        logger.error("Failed to draw Obico detection boxes", exc_info=True)
        return frame


async def _notify(
    printer_id: int,
    printer_name: str,
    task_name: str,
    score: float,
    action: str,
    frame: bytes | None = None,
    detections: list | None = None,
) -> None:
    from backend.app.services.notification_service import notification_service

    detail = (
        f"Possible print failure detected on '{task_name or 'current job'}' "
        f"(confidence {score:.2f}). Action taken: {action}."
    )
    image_data = _draw_detection_boxes(frame, detections)
    async with async_session() as db:
        try:
            await notification_service.on_printer_error(
                printer_id=printer_id,
                printer_name=printer_name,
                error_type="ai_failure_detection",
                db=db,
                error_detail=detail,
                image_data=image_data,
            )
        except Exception as e:
            logger.error("Obico notify failed for printer %s: %s", printer_id, e)
