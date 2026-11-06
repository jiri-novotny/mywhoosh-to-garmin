"""FIT file service for handling file modifications."""

import os
import tempfile
import logging
from typing import Optional
from fit_tool.data_message import DataMessage
from fit_tool.definition_message import DefinitionMessage
from fit_tool.fit_file import FitFile
from fit_tool.profile.messages.device_info_message import DeviceInfoMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.profile_type import Manufacturer, GarminProduct
from fit_tool.fit_file_builder import FitFileBuilder


class FitFileService:
    """Service for modifying FIT files."""

    def __init__(self, serial_number: Optional[int] = None):
        """Initialize FitFileService.

        Args:
            serial_number: Default device serial number to write into FIT files.
                If None, the serial number from the original file is kept.
        """
        self.logger = logging.getLogger(__name__)
        self.serial_number = self._validate_serial_number(serial_number)

    @staticmethod
    def _validate_serial_number(serial_number: Optional[int]) -> Optional[int]:
        """Validate a serial number against the FIT uint32z range.

        Raises:
            ValueError: If the serial number is out of range
        """
        if serial_number is None:
            return None
        serial_number = int(serial_number)
        if not 0 < serial_number <= 0xFFFFFFFF:
            raise ValueError(
                f"Invalid serial number {serial_number}: must be between 1 and {0xFFFFFFFF}"
            )
        return serial_number

    @staticmethod
    def _set_serial_number(message, serial_number: int) -> None:
        """Set serial_number on a message, adding the field if the original file lacks it.

        fit_tool's property setter silently does nothing when the field is not part
        of the message definition read from the file (it is absent or a non-growable
        placeholder), so add it from a fresh template.
        """
        field = message.get_field_by_name('serial_number')
        if field is None or (field.length == 0 and not field.growable):
            template_field = type(message)().get_field_by_name('serial_number')
            if field is not None:
                message.fields.remove(field)
            message.fields.append(template_field)
            message.serial_number = serial_number
            # The original definition doesn't include the new field; drop it so
            # FitFileBuilder (auto_define) emits a matching definition message.
            message.definition_message = None
        else:
            message.serial_number = serial_number

    def modify_device_info(self, fit_file_path: str,
                          manufacturer: Optional[int] = None,
                          product: Optional[int] = None,
                          software_version: Optional[float] = None,
                          serial_number: Optional[int] = None) -> str:
        """Modifies the device manufacturer and type in a .fit file.

        Args:
            fit_file_path: Path to the original FIT file
            manufacturer: Device manufacturer (defaults to Garmin - ID 1)
            product: Device product (defaults to Enduro 3 - ID 4575)
            software_version: Software version (defaults to 23.39)
            serial_number: Device serial number (defaults to the value passed to
                the constructor; if neither is set, the original serial is kept)

        Returns:
            Path to the modified FIT file

        Raises:
            FileNotFoundError: If the input file doesn't exist
            RuntimeError: If file modification fails
        """
        if not os.path.exists(fit_file_path):
            raise FileNotFoundError(f"FIT file not found: {fit_file_path}")

        # Defaults: Garmin Enduro 3
        manufacturer = manufacturer or Manufacturer.GARMIN.value
        product = product or 4575
        software_version = software_version or 23.39
        serial_number = (self._validate_serial_number(serial_number)
                         if serial_number is not None else self.serial_number)

        self.logger.info(f"Modifying FIT file: {fit_file_path}")
        self.logger.info(
            f"Target device: "
            f"(manufacturer: {manufacturer}, product: {product}, sw: {software_version}, "
            f"serial: {serial_number if serial_number is not None else 'unchanged'})"
        )

        try:
            content = FitFile.from_file(fit_file_path)

            # Set auto_define to true, so that the builder creates the required Definition Messages
            builder = FitFileBuilder(auto_define=True, min_string_size=50)

            # Local message types whose definition we changed by adding a field.
            # Later data messages of that local type still carry the original
            # definition, so it must be re-derived until the file redefines it.
            redefined_local_ids = set()

            for record in content.records:
                message = record.message
                if isinstance(message, DefinitionMessage):
                    redefined_local_ids.discard(message.local_id)
                elif message.local_id in redefined_local_ids:
                    message.definition_message = None

                if isinstance(message, FileIdMessage):
                    message.manufacturer = manufacturer
                    message.product = product
                    if serial_number is not None:
                        self._set_serial_number(message, serial_number)
                elif isinstance(message, DeviceInfoMessage):
                    message.manufacturer = manufacturer
                    message.product = product
                    message.software_version = software_version
                    # Only the creator device (index 0) gets the serial number;
                    # connected sensors keep their own.
                    if serial_number is not None and message.device_index in (None, 0):
                        self._set_serial_number(message, serial_number)

                if isinstance(message, DataMessage) and message.definition_message is None:
                    redefined_local_ids.add(message.local_id)
                builder.add(message)

            # Build the FIT file object
            fit_file = builder.build()

            # Save the modified FIT file
            temp_dir = tempfile.gettempdir()
            modified_fit_file_path = os.path.join(temp_dir, "modified_" + os.path.basename(fit_file_path))
            fit_file.to_file(modified_fit_file_path)

            self.logger.info(f"Modified FIT file saved to {modified_fit_file_path}")
            return modified_fit_file_path

        except Exception as e:
            raise RuntimeError(f"Failed to modify FIT file: {e}") from e

    def cleanup_file(self, file_path: str) -> None:
        """Clean up a temporary file.

        Args:
            file_path: Path to the file to remove
        """
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                self.logger.info(f"Cleaned up file: {file_path}")
        except OSError as e:
            self.logger.warning(f"Failed to cleanup file {file_path}: {e}")
