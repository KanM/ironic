# Copyright 2015 Hewlett-Packard Development Company, L.P.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
"""
Boot Interface for iLO drivers and its supporting methods.
"""

import os
import tempfile

from ironic_lib import utils as ironic_utils
from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import excutils
import six.moves.urllib.parse as urlparse

from ironic.common import boot_devices
from ironic.common import exception
from ironic.common.glance_service import service_utils
from ironic.common.i18n import _
from ironic.common.i18n import _LE
from ironic.common.i18n import _LW
from ironic.common import image_service
from ironic.common import images
from ironic.common import swift
from ironic.conductor import utils as manager_utils
from ironic.drivers import base
from ironic.drivers.modules import deploy_utils
from ironic.drivers.modules.ilo import common as ilo_common

LOG = logging.getLogger(__name__)

CONF = cfg.CONF

REQUIRED_PROPERTIES = {
    'ilo_deploy_iso': _("UUID (from Glance) of the deployment ISO. "
                        "Required.")
}
COMMON_PROPERTIES = REQUIRED_PROPERTIES


def parse_driver_info(node):
    """Gets the driver specific Node deployment info.

    This method validates whether the 'driver_info' property of the
    supplied node contains the required information for this driver to
    deploy images to the node.

    :param node: a single Node.
    :returns: A dict with the driver_info values.
    :raises: MissingParameterValue, if any of the required parameters are
        missing.
    """
    info = node.driver_info
    d_info = {}
    d_info['ilo_deploy_iso'] = info.get('ilo_deploy_iso')

    error_msg = _("Error validating iLO virtual media deploy. Some parameters"
                  " were missing in node's driver_info")
    deploy_utils.check_for_missing_params(d_info, error_msg)

    return d_info


def _get_boot_iso_object_name(node):
    """Returns the boot iso object name for a given node.

    :param node: the node for which object name is to be provided.
    """
    return "boot-%s" % node.uuid


def _get_boot_iso(task, root_uuid):
    """This method returns a boot ISO to boot the node.

    It chooses one of the three options in the order as below:
    1. Does nothing if 'ilo_boot_iso' is present in node's instance_info and
       'boot_iso_created_in_web_server' is not set in 'driver_internal_info'.
    2. Image deployed has a meta-property 'boot_iso' in Glance. This should
       refer to the UUID of the boot_iso which exists in Glance.
    3. Generates a boot ISO on the fly using kernel and ramdisk mentioned in
       the image deployed. It uploads the generated boot ISO to Swift.

    :param task: a TaskManager instance containing the node to act on.
    :param root_uuid: the uuid of the root partition.
    :returns: boot ISO URL. Should be either of below:
        * A Swift object - It should be of format 'swift:<object-name>'. It is
          assumed that the image object is present in
          CONF.ilo.swift_ilo_container;
        * A Glance image - It should be format 'glance://<glance-image-uuid>'
          or just <glance-image-uuid>;
        * An HTTP URL.
        On error finding the boot iso, it returns None.
    :raises: MissingParameterValue, if any of the required parameters are
        missing in the node's driver_info or instance_info.
    :raises: InvalidParameterValue, if any of the parameters have invalid
        value in the node's driver_info or instance_info.
    :raises: SwiftOperationError, if operation with Swift fails.
    :raises: ImageCreationFailed, if creation of boot ISO failed.
    :raises: exception.ImageRefValidationFailed if ilo_boot_iso is not
        HTTP(S) URL.
    """
    LOG.debug("Trying to get a boot ISO to boot the baremetal node")

    # Option 1 - Check if user has provided ilo_boot_iso in node's
    # instance_info
    driver_internal_info = task.node.driver_internal_info
    boot_iso_created_in_web_server = (
        driver_internal_info.get('boot_iso_created_in_web_server'))

    if (task.node.instance_info.get('ilo_boot_iso')
            and not boot_iso_created_in_web_server):
        LOG.debug("Using ilo_boot_iso provided in node's instance_info")
        boot_iso = task.node.instance_info['ilo_boot_iso']
        if not service_utils.is_glance_image(boot_iso):
            try:
                image_service.HttpImageService().validate_href(boot_iso)
            except exception.ImageRefValidationFailed:
                with excutils.save_and_reraise_exception():
                    LOG.error(_LE("Virtual media deploy accepts only Glance "
                                  "images or HTTP(S) URLs as "
                                  "instance_info['ilo_boot_iso']. Either %s "
                                  "is not a valid HTTP(S) URL or is "
                                  "not reachable."), boot_iso)

        return task.node.instance_info['ilo_boot_iso']

    # Option 2 - Check if user has provided a boot_iso in Glance. If boot_iso
    # is a supported non-glance href execution will proceed to option 3.
    deploy_info = _parse_deploy_info(task.node)

    image_href = deploy_info['image_source']
    image_properties = (
        images.get_image_properties(
            task.context, image_href, ['boot_iso', 'kernel_id', 'ramdisk_id']))

    boot_iso_uuid = image_properties.get('boot_iso')
    kernel_href = (task.node.instance_info.get('kernel') or
                   image_properties.get('kernel_id'))
    ramdisk_href = (task.node.instance_info.get('ramdisk') or
                    image_properties.get('ramdisk_id'))

    if boot_iso_uuid:
        LOG.debug("Found boot_iso %s in Glance", boot_iso_uuid)
        return boot_iso_uuid

    if not kernel_href or not ramdisk_href:
        LOG.error(_LE("Unable to find kernel or ramdisk for "
                      "image %(image)s to generate boot ISO for %(node)s"),
                  {'image': image_href, 'node': task.node.uuid})
        return

    # NOTE(rameshg87): Functionality to share the boot ISOs created for
    # similar instances (instances with same deployed image) is
    # not implemented as of now. Creation/Deletion of such a shared boot ISO
    # will require synchronisation across conductor nodes for the shared boot
    # ISO.  Such a synchronisation mechanism doesn't exist in ironic as of now.

    # Option 3 - Create boot_iso from kernel/ramdisk, upload to Swift
    # or web server and provide its name.
    deploy_iso_uuid = deploy_info['ilo_deploy_iso']
    boot_mode = deploy_utils.get_boot_mode_for_deploy(task.node)
    boot_iso_object_name = _get_boot_iso_object_name(task.node)
    kernel_params = CONF.pxe.pxe_append_params
    with tempfile.NamedTemporaryFile(dir=CONF.tempdir) as fileobj:
        boot_iso_tmp_file = fileobj.name
        images.create_boot_iso(task.context, boot_iso_tmp_file,
                               kernel_href, ramdisk_href,
                               deploy_iso_uuid, root_uuid,
                               kernel_params, boot_mode)

        if CONF.ilo.use_web_server_for_images:
            boot_iso_url = (
                ilo_common.copy_image_to_web_server(boot_iso_tmp_file,
                                                    boot_iso_object_name))
            driver_internal_info = task.node.driver_internal_info
            driver_internal_info['boot_iso_created_in_web_server'] = True
            task.node.driver_internal_info = driver_internal_info
            task.node.save()
            LOG.debug("Created boot_iso %(boot_iso)s for node %(node)s",
                      {'boot_iso': boot_iso_url, 'node': task.node.uuid})
            return boot_iso_url
        else:
            container = CONF.ilo.swift_ilo_container
            swift_api = swift.SwiftAPI()
            swift_api.create_object(container, boot_iso_object_name,
                                    boot_iso_tmp_file)

            LOG.debug("Created boot_iso %s in Swift", boot_iso_object_name)
            return 'swift:%s' % boot_iso_object_name


def _clean_up_boot_iso_for_instance(node):
    """Deletes the boot ISO if it was created for the instance.

    :param node: an ironic node object.
    """
    ilo_boot_iso = node.instance_info.get('ilo_boot_iso')
    if not ilo_boot_iso:
        return
    if ilo_boot_iso.startswith('swift'):
        swift_api = swift.SwiftAPI()
        container = CONF.ilo.swift_ilo_container
        boot_iso_object_name = _get_boot_iso_object_name(node)
        try:
            swift_api.delete_object(container, boot_iso_object_name)
        except exception.SwiftOperationError as e:
            LOG.exception(_LE("Failed to clean up boot ISO for node "
                              "%(node)s. Error: %(error)s."),
                          {'node': node.uuid, 'error': e})
    elif CONF.ilo.use_web_server_for_images:
        result = urlparse.urlparse(ilo_boot_iso)
        ilo_boot_iso_name = os.path.basename(result.path)
        boot_iso_path = os.path.join(
            CONF.deploy.http_root, ilo_boot_iso_name)
        ironic_utils.unlink_without_raise(boot_iso_path)


def _parse_deploy_info(node):
    """Gets the instance and driver specific Node deployment info.

    This method validates whether the 'instance_info' and 'driver_info'
    property of the supplied node contains the required information for
    this driver to deploy images to the node.

    :param node: a single Node.
    :returns: A dict with the instance_info and driver_info values.
    :raises: MissingParameterValue, if any of the required parameters are
        missing.
    :raises: InvalidParameterValue, if any of the parameters have invalid
        value.
    """
    info = {}
    info.update(deploy_utils.get_image_instance_info(node))
    info.update(parse_driver_info(node))
    return info


class IloVirtualMediaBoot(base.BootInterface):

    def get_properties(self):
        return COMMON_PROPERTIES

    def validate(self, task):
        """Validate the deployment information for the task's node.

        :param task: a TaskManager instance containing the node to act on.
        :raises: InvalidParameterValue, if some information is invalid.
        :raises: MissingParameterValue if 'kernel_id' and 'ramdisk_id' are
            missing in the Glance image or 'kernel' and 'ramdisk' not provided
            in instance_info for non-Glance image.
        """

        node = task.node

        d_info = _parse_deploy_info(node)

        if node.driver_internal_info.get('is_whole_disk_image'):
            props = []
        elif service_utils.is_glance_image(d_info['image_source']):
            props = ['kernel_id', 'ramdisk_id']
        else:
            props = ['kernel', 'ramdisk']
        deploy_utils.validate_image_properties(task.context, d_info, props)

    def prepare_ramdisk(self, task, ramdisk_params):
        """Prepares the boot of deploy ramdisk using virtual media.

        This method prepares the boot of the deploy ramdisk after
        reading relevant information from the node's driver_info and
        instance_info.

        :param task: a task from TaskManager.
        :param ramdisk_params: the parameters to be passed to the ramdisk.
        :returns: None
        :raises: MissingParameterValue, if some information is missing in
            node's driver_info or instance_info.
        :raises: InvalidParameterValue, if some information provided is
            invalid.
        :raises: IronicException, if some power or set boot boot device
            operation failed on the node.
        :raises: IloOperationError, if some operation on iLO failed.
        """

        node = task.node

        # Clear ilo_boot_iso if it's a glance image to force recreate
        # another one again (or use existing one in glance).
        # This is mainly for rebuild scenario.
        if service_utils.is_glance_image(
                node.instance_info.get('image_source')):
            instance_info = node.instance_info
            instance_info.pop('ilo_boot_iso', None)
            node.instance_info = instance_info
            node.save()

        # Eject all virtual media devices, as we are going to use them
        # during deploy.
        ilo_common.eject_vmedia_devices(task)

        deploy_nic_mac = deploy_utils.get_single_nic_with_vif_port_id(task)
        ramdisk_params['BOOTIF'] = deploy_nic_mac
        deploy_iso = node.driver_info['ilo_deploy_iso']

        ilo_common.setup_vmedia(task, deploy_iso, ramdisk_params)

    def prepare_instance(self, task):
        """Prepares the boot of instance.

        This method prepares the boot of the instance after reading
        relevant information from the node's instance_info.
        It does the following depending on boot_option for deploy:

        - If the boot_option requested for this deploy is 'local' or image
          is a whole disk image, then it sets the node to boot from disk.
        - Otherwise it finds/creates the boot ISO to boot the instance
          image, attaches the boot ISO to the bare metal and then sets
          the node to boot from CDROM.

        :param task: a task from TaskManager.
        :returns: None
        :raises: IloOperationError, if some operation on iLO failed.
        """

        ilo_common.cleanup_vmedia_boot(task)

        # For iscsi_ilo driver, we boot from disk every time if the image
        # deployed is a whole disk image.
        node = task.node
        iwdi = node.driver_internal_info.get('is_whole_disk_image')
        if deploy_utils.get_boot_option(node) == "local" or iwdi:
            manager_utils.node_set_boot_device(task, boot_devices.DISK,
                                               persistent=True)
        else:
            drv_int_info = node.driver_internal_info
            root_uuid_or_disk_id = drv_int_info.get('root_uuid_or_disk_id')
            if root_uuid_or_disk_id:
                self._configure_vmedia_boot(task, root_uuid_or_disk_id)
            else:
                LOG.warning(_LW("The UUID for the root partition could not "
                                "be found for node %s"), node.uuid)

    def clean_up_instance(self, task):
        """Cleans up the boot of instance.

        This method cleans up the environment that was setup for booting
        the instance. It ejects virtual media

        :param task: a task from TaskManager.
        :returns: None
        :raises: IloOperationError, if some operation on iLO failed.
        """

        _clean_up_boot_iso_for_instance(task.node)

        driver_internal_info = task.node.driver_internal_info
        driver_internal_info.pop('boot_iso_created_in_web_server', None)
        driver_internal_info.pop('root_uuid_or_disk_id', None)
        task.node.driver_internal_info = driver_internal_info
        task.node.save()

        ilo_common.cleanup_vmedia_boot(task)

    def clean_up_ramdisk(self, task):
        """Cleans up the boot of ironic ramdisk.

        This method cleans up virtual media devices setup for the deploy
        ramdisk.

        :param task: a task from TaskManager.
        :returns: None
        :raises: IloOperationError, if some operation on iLO failed.
        """

        ilo_common.cleanup_vmedia_boot(task)

    def _configure_vmedia_boot(self, task, root_uuid):
        """Configure vmedia boot for the node.

        :param task: a task from TaskManager.
        :param root_uuid: uuid of the root partition
        :returns: None
        :raises: IloOperationError, if some operation on iLO failed.
        """

        node = task.node
        boot_iso = _get_boot_iso(task, root_uuid)
        if not boot_iso:
            LOG.error(_LE("Cannot get boot ISO for node %s"), node.uuid)
            return

        # Upon deploy complete, some distros cloud images reboot the system as
        # part of its configuration. Hence boot device should be persistent and
        # not one-time.
        ilo_common.setup_vmedia_for_boot(task, boot_iso)
        manager_utils.node_set_boot_device(task,
                                           boot_devices.CDROM,
                                           persistent=True)

        i_info = node.instance_info
        i_info['ilo_boot_iso'] = boot_iso
        node.instance_info = i_info
        node.save()
