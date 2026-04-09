import os
import fcntl
from lxml import etree
from django.conf import settings
from threading import Lock
from voice_orchestrator.freeswitch import fs_manager

_xml_lock = Lock()

DEFAULT_XML = os.path.join(settings.FS_DIR, "default.xml")

def fs_create_user(extension, password, group):
    # 1. Write the user XML file
    user_xml = f"""<include>
      <user id="{extension}">
        <params>
          <param name="password" value="{password}"/>
        </params>
        <variables>
          <variable name="user_context" value="default"/>
          <variable name="effective_caller_id_number" value="{extension}"/>
          <variable name="media_webrtc" value="true"/>
          <variable name="rtp_secure_media" value="mandatory:AES_CM_128_HMAC_SHA1_80"/>
        </variables>
      </user>
    </include>"""

    user_file = os.path.join(settings.FS_DIR, "default", f"{extension}.xml")
    with open(user_file, 'w') as f:
        f.write(user_xml)

    # 2. Add pointer to the group in default.xml
    fs_add_user_to_group(extension, group)

    # 3. Reload FreeSWITCH
    fs_manager.api('reloadxml')


def fs_add_user_to_group(extension, group_name):
    tree = etree.parse(DEFAULT_XML)
    root = tree.getroot()

    # Find the target group
    groups = root.findall(f".//group[@name='{group_name}']")
    if not groups:
        raise ValueError(f"Group {group_name} not found")

    group = groups[0]
    users_el = group.find('users')

    # Check if pointer already exists
    existing = users_el.findall(f"user[@id='{extension}']")
    if existing:
        return  # already there

    # Add the pointer
    new_user = etree.SubElement(users_el, 'user')
    new_user.set('id', str(extension))
    new_user.set('type', 'pointer')

    # Write back
    tree.write(DEFAULT_XML, pretty_print=True, xml_declaration=True, encoding='UTF-8')

def fs_move_user(extension, from_group, to_group):
    with _xml_lock:
        tree = etree.parse(DEFAULT_XML)
        root = tree.getroot()

        # Remove from old group
        old_group = root.find(f".//group[@name='{from_group}']/users")
        for u in old_group.findall(f"user[@id='{extension}']"):
            old_group.remove(u)

        # Add to new group
        new_group = root.find(f".//group[@name='{to_group}']/users")
        new_user = etree.SubElement(new_group, 'user')
        new_user.set('id', str(extension))
        new_user.set('type', 'pointer')

        tree.write(DEFAULT_XML, pretty_print=True, xml_declaration=True, encoding='UTF-8')

    fs_manager.api('reloadxml')

def fs_delete_user(extension):
    # Remove user file
    user_file = os.path.join(settings.FS_DIR, "default", f"{extension}.xml")
    if os.path.exists(user_file):
        os.remove(user_file)

    # Remove pointer from all groups
    with _xml_lock:
        tree = etree.parse(DEFAULT_XML)
        root = tree.getroot()
        for user_el in root.findall(f".//user[@id='{extension}'][@type='pointer']"):
            user_el.getparent().remove(user_el)
        tree.write(DEFAULT_XML, pretty_print=True, xml_declaration=True, encoding='UTF-8')

    fs_manager.api('reloadxml')