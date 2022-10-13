#
# Copyright 2018-2022 Elyra Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from enum import Enum
from importlib import import_module
import json
import re
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from typing import TYPE_CHECKING

# Prevent a circular reference by importing RuntimePipelineProcessor only during type-checking
if TYPE_CHECKING:
    from elyra.pipeline.processor import RuntimePipelineProcessor

from elyra.pipeline.pipeline_constants import DISABLE_NODE_CACHING
from elyra.pipeline.pipeline_constants import ENV_VARIABLES
from elyra.pipeline.pipeline_constants import KUBERNETES_POD_ANNOTATIONS
from elyra.pipeline.pipeline_constants import KUBERNETES_POD_LABELS
from elyra.pipeline.pipeline_constants import KUBERNETES_SECRETS
from elyra.pipeline.pipeline_constants import KUBERNETES_TOLERATIONS
from elyra.pipeline.pipeline_constants import MOUNTED_VOLUMES
from elyra.util.kubernetes import is_valid_annotation_key
from elyra.util.kubernetes import is_valid_annotation_value
from elyra.util.kubernetes import is_valid_kubernetes_key
from elyra.util.kubernetes import is_valid_kubernetes_resource_name
from elyra.util.kubernetes import is_valid_label_key
from elyra.util.kubernetes import is_valid_label_value


class PropertyAttribute:
    """
    An attribute of an ElyraProperty instance that provides the means to construct the
    schema for a property and contains information for processing property instances.
    """

    _input_type_to_default_value = {"boolean": False, "array": "[]", "object": "{}", "string": ""}

    def __init__(
        self,
        attribute_id: str,
        display_name: Optional[str] = None,
        placeholder: Optional[Any] = None,
        default_value: Optional[Any] = None,
        input_type: Optional[str] = None,
        enum: Optional[List[Any]] = None,
        hidden: Optional[bool] = False,
        required: Optional[bool] = False,
    ):
        """
        :param attribute_id: a shorthand id for this attribute, e.g. "env_var"
        :param display_name: the display name for this attribute
        :param placeholder: a placeholder value to be shown in the input field for this attribute
        :param default_value: the default value to assign the attribute
        :param input_type: the JSON data type of this attribute ("string", "boolean", "number", "array", or "object")
        :param enum: a list of possible values that this attribute can take (will appear in a dropdown menu)
        :param hidden: whether this attribute should be hidden in the UI, preventing users from entering a value
        :param required: whether a value for this attribute is required
        """
        self.id = attribute_id
        self.title = display_name
        self.placeholder = placeholder
        self.default_value = default_value or self._input_type_to_default_value.get(input_type)
        self.input_type = input_type
        self.enum = enum
        self.hidden = hidden
        self.required = required


class ListItemPropertyAttribute(PropertyAttribute):
    """
    An attribute of an ElyraPropertyListItem instance that provides the means to construct the
    schema for a property and contains information for processing property instances.
    """

    def __init__(
        self,
        attribute_id: str,
        display_name: Optional[str] = None,
        placeholder: Optional[Any] = None,
        default_value: Optional[Any] = None,
        input_type: Optional[str] = None,
        enum: Optional[List[Any]] = None,
        hidden: Optional[bool] = False,
        required: Optional[bool] = False,
        use_in_key: Optional[bool] = True,
    ):
        """
        :param use_in_key: whether this attribute should be used when constructing a
            key for an instance that will be used to de-duplicate list items
        """
        super().__init__(attribute_id, display_name, placeholder, default_value, input_type, enum, hidden, required)
        self.use_in_key = use_in_key


class ElyraProperty(ABC):
    """A component property that is defined and processed by Elyra"""

    applies_to_generic: bool  # True if the property applies to generic components
    applies_to_custom: bool  # True if the property applies to custom components

    property_id: str
    property_display_name: str
    property_description: str
    property_attributes: List[PropertyAttribute] = []
    _json_data_type: str = None
    _required: bool = False

    _subclass_property_map: Dict[str, type] = {}

    @classmethod
    def all_subclasses(cls):
        """Get all nested subclasses for a class."""
        return set(cls.__subclasses__()).union([s for c in cls.__subclasses__() for s in c.all_subclasses()])

    @classmethod
    def build_property_map(cls) -> None:
        """Build the map of property subclasses."""
        cls._subclass_property_map = {sc.property_id: sc for sc in cls.all_subclasses() if hasattr(sc, "property_id")}

    @classmethod
    def get_single_instance(cls, value: Optional[Dict[str, Any]] = None) -> ElyraProperty | None:
        """Unpack values from dictionary object and instantiate a class instance."""
        if not isinstance(value, dict):
            value = {}

        params = {attr.id: cls.strip_if_string(value.get(attr.id)) for attr in cls.property_attributes}
        instance = getattr(import_module(cls.__module__), cls.__name__)(**params)
        return None if instance.should_discard() else instance

    @classmethod
    def create_instance(cls, prop_id: str, value: Optional[Any]) -> ElyraProperty | ElyraPropertyList | None:
        """Create an instance of a class with the given property id using the user-entered values."""
        if not cls._subclass_property_map:
            cls.build_property_map()

        sc = cls._subclass_property_map.get(prop_id)
        if issubclass(sc, ElyraPropertyListItem):
            if not isinstance(value, list):
                return None
            instances = [sc.get_single_instance(obj) for obj in value]  # create instance for each object
            return ElyraPropertyList(instances).deduplicate()  # convert to ElyraPropertyList and de-dupe
        elif issubclass(sc, ElyraProperty):
            return sc.get_single_instance(value)

        return None

    @classmethod
    def get_classes_for_component_type(cls, component_type: str, runtime_type: Optional[str] = "") -> Set[type]:
        """
        Retrieve property subclasses that apply to the given component type
        (e.g., custom or generic) and to the given runtime type.
        """
        from elyra.pipeline.processor import PipelineProcessorManager  # placed here to avoid circular reference

        processor_props = set()
        for processor in PipelineProcessorManager.instance().get_all_processors():
            props = getattr(processor, "supported_properties", set())
            if processor.type.name == runtime_type and props:
                processor_props = props  # correct processor is found, and it explicitly specifies its properties
                break
            processor_props.update(props)

        all_subclasses = set()
        for sc in cls.all_subclasses():
            sc_id = getattr(sc, "property_id", "")
            if sc_id in processor_props and getattr(sc, f"applies_to_{component_type}", False):
                all_subclasses.add(sc)

        return all_subclasses

    @classmethod
    def get_schema(cls) -> Dict[str, Any]:
        """
        Build the JSON schema for an Elyra-owned component property using the attributes
        defined in the human-readable list of PropertyAttribute object for each class.
        """
        class_description = re.sub(" +", " ", cls.property_description.replace("\n", " ")).strip()
        schema = {"title": cls.property_display_name, "description": class_description, "type": cls._json_data_type}
        if cls._json_data_type is not None:  # property is a scalar value  TODO deprecate when able
            return schema

        properties, uihints, required_list = {}, {}, []
        for attr in cls.property_attributes:
            if attr.hidden:
                continue

            properties[attr.id] = {"type": attr.input_type, "title": attr.title or attr.id}
            if attr.default_value is not None:
                properties[attr.id]["default"] = attr.default_value
            if attr.enum:
                properties[attr.id]["enum"] = attr.enum
            if attr.placeholder:
                uihints[attr.id] = {"ui:placeholder": attr.placeholder}
            if attr.required:
                required_list.append(attr.id)

        if issubclass(cls, ElyraPropertyListItem):
            items = {"type": "object", "properties": properties, "required": required_list}
            schema.update({"type": "array", "default": [], "items": items, "uihints": {"items": uihints}})
        else:
            schema.update({"type": "object", "properties": properties, "required": required_list, "uihints": uihints})

        return schema

    @staticmethod
    def strip_if_string(var: Any) -> Any:
        """Strip surrounding whitespace from variable if it is a string"""
        return var.strip() if isinstance(var, str) else var

    def should_discard(self) -> bool:
        """
        Returns a boolean indicating whether an instance should be silently discarded on
        the basis of its attribute values. A discarded instance will not be validated or
        processed.

        Override this method if there are any constraints that dictate that this instance
        should not be processed.
        """
        return False

    @abstractmethod
    def get_value_for_display(self) -> Dict[str, Any]:
        """
        Get a representation of the instance to display in UI error messages.
        Should be implemented in any subclass that has validation criteria.
        """
        pass

    @abstractmethod
    def get_all_validation_errors(self) -> List[str]:
        """Perform custom validation on an instance."""
        pass

    @abstractmethod
    def add_to_execution_object(self, runtime_processor: RuntimePipelineProcessor, execution_object: Any, **kwargs):
        """
        Add a property instance to the execution object for the given runtime processor.
        Calls the runtime processor's implementation of add_{property_type}, e.g.
        runtime_processor.add_kubernetes_secret(self, execution_object, **kwargs).
        """
        pass


class DisableNodeCaching(ElyraProperty):
    """An ElyraProperty representing node cache preference"""

    applies_to_generic = False
    applies_to_custom = True

    property_id = DISABLE_NODE_CACHING
    property_display_name = "Disable node caching"
    property_description = "Disable caching to force node re-execution in the target runtime environment."
    _json_data_type = "string"

    def __init__(self, selection, **kwargs):
        self.selection = selection == "True"

    @classmethod
    def get_single_instance(cls, value: Optional[Any] = None) -> ElyraProperty | None:
        return DisableNodeCaching(selection=value)

    @classmethod
    def get_schema(cls) -> Dict[str, Any]:
        """Build the JSON schema for an Elyra-owned component property"""
        schema = super().get_schema()
        schema["enum"] = ["True", "False"]
        schema["uihints"] = {"ui:placeholder": "Use runtime default"}
        return schema

    def get_value_for_display(self) -> Dict[str, Any]:
        return self.selection

    def get_all_validation_errors(self) -> List[str]:
        return []

    def add_to_execution_object(self, runtime_processor: RuntimePipelineProcessor, execution_object: Any, **kwargs):
        """Add DisableNodeCaching info to the execution object for the given runtime processor"""
        runtime_processor.add_disable_node_caching(instance=self, execution_object=execution_object, **kwargs)


class ElyraPropertyListItem(ElyraProperty):
    """
    An Elyra-owned property that is meant to be a member of an ElyraOwnedPropertyList.
    """

    property_attributes: List[ListItemPropertyAttribute] = []

    def to_dict(self) -> Dict[str, Any]:
        """Convert instance to a dict with relevant class attributes."""
        dict_repr = {attr.id: getattr(self, attr.id, None) for attr in self.property_attributes}
        return dict_repr

    def get_key_for_dict_entry(self) -> str:
        """
        Given the attribute names in the 'key' property, construct a key
        based on the attribute values of the instance.
        """
        prop_key = ""
        keys = [attr.id for attr in self.property_attributes if attr.use_in_key]
        for key_attr in keys:
            key_part = getattr(self, key_attr)
            if key_part:
                prop_key += f"{key_part}:" if key_attr != keys[-1] else key_part
        return prop_key

    def get_value_for_dict_entry(self) -> str:
        """Returns the value to be used when constructing a dict from a list of classes."""
        return self.to_dict()

    def get_value_for_display(self) -> Dict[str, Any]:
        """Get a representation of the instance to display in UI error messages."""
        dict_repr = self.to_dict()
        for attr in self.property_attributes:
            if attr.hidden:
                dict_repr.pop(attr.id)
        return dict_repr


class EnvironmentVariable(ElyraPropertyListItem):
    """An ElyraProperty representing a single Environment Variable"""

    applies_to_generic = True
    applies_to_custom = False

    property_id = ENV_VARIABLES
    property_display_name = "Environment Variables"
    property_description = "Environment variables to be set on the execution environment."
    property_attributes = [
        ListItemPropertyAttribute(
            attribute_id="env_var",
            display_name="Environment Variable",
            placeholder="ENV_VAR",
            input_type="string",
            hidden=False,
            required=True,
            use_in_key=True,
        ),
        ListItemPropertyAttribute(
            attribute_id="value",
            display_name="Value",
            placeholder="value",
            input_type="string",
            hidden=False,
            required=False,
            use_in_key=False,
        ),
    ]

    def __init__(self, env_var, value, **kwargs):
        self.env_var = env_var
        self.value = value

    @classmethod
    def get_schema(cls) -> Dict[str, Any]:
        """Build the JSON schema for an Elyra-owned component property"""
        schema = super().get_schema()
        schema["uihints"].update({"canRefresh": True})
        return schema

    def get_value_for_dict_entry(self) -> str:
        """Returns the value to be used when constructing a dict from a list of classes."""
        return self.value

    def should_discard(self) -> bool:
        """If a value is not specified, this EnvironmentVariable instance should be silently ignored."""
        return not self.value

    def get_all_validation_errors(self) -> List[str]:
        """Perform custom validation on an instance."""
        validation_errors = []
        if not self.env_var:
            validation_errors.append("Required environment variable was not specified.")
        elif " " in self.env_var:
            validation_errors.append(f"Environment variable '{self.env_var}' includes invalid space character(s).")

        return validation_errors

    def add_to_execution_object(self, runtime_processor: RuntimePipelineProcessor, execution_object: Any, **kwargs):
        """Add EnvironmentVariable instance to the execution object for the given runtime processor"""
        runtime_processor.add_env_var(instance=self, execution_object=execution_object, **kwargs)


class KubernetesSecret(ElyraPropertyListItem):
    """An ElyraProperty representing a single Kubernetes secret"""

    applies_to_generic = True
    applies_to_custom = False

    property_id = KUBERNETES_SECRETS
    property_display_name = "Kubernetes Secrets"
    property_description = """Kubernetes secrets to make available as environment
    variables to this node. The secret name and key given must be present in the
    Kubernetes namespace where the node is executed or this node will not run."""
    property_attributes = [
        ListItemPropertyAttribute(
            attribute_id="env_var",
            display_name="Environment Variable",
            placeholder="ENV_VAR",
            input_type="string",
            hidden=False,
            required=True,
            use_in_key=True,
        ),
        ListItemPropertyAttribute(
            attribute_id="name",
            display_name="Secret Name",
            placeholder="secret-name",
            input_type="string",
            hidden=False,
            required=True,
            use_in_key=False,
        ),
        ListItemPropertyAttribute(
            attribute_id="key",
            display_name="Secret Key",
            placeholder="secret-key",
            input_type="string",
            hidden=False,
            required=True,
            use_in_key=False,
        ),
    ]

    def __init__(self, env_var, name, key, **kwargs):
        self.env_var = env_var
        self.name = name
        self.key = key

    def get_all_validation_errors(self) -> List[str]:
        """Perform custom validation on an instance."""
        validation_errors = []
        if not self.env_var:
            validation_errors.append("Required environment variable was not specified.")
        if not self.name:
            validation_errors.append("Required secret name was not specified.")
        elif not is_valid_kubernetes_resource_name(self.name):
            validation_errors.append(
                f"Secret name '{self.name}' is not a valid Kubernetes resource name.",
            )
        if not self.key:
            validation_errors.append("Required secret key was not specified.")
        elif not is_valid_kubernetes_key(self.key):
            validation_errors.append(
                f"Key '{self.key}' is not a valid Kubernetes secret key.",
            )

        return validation_errors

    def add_to_execution_object(self, runtime_processor: RuntimePipelineProcessor, execution_object: Any, **kwargs):
        """Add KubernetesSecret instance to the execution object for the given runtime processor"""
        runtime_processor.add_kubernetes_secret(instance=self, execution_object=execution_object, **kwargs)


class VolumeMount(ElyraPropertyListItem):
    """An ElyraProperty representing a single PVC"""

    applies_to_generic = True
    applies_to_custom = True

    property_id = MOUNTED_VOLUMES
    property_display_name = "Data Volumes"
    property_description = """Volumes to be mounted in this node. The specified Persistent Volume Claims
    must exist in the Kubernetes namespace where the node is executed or this node will not run."""
    property_attributes = [
        ListItemPropertyAttribute(
            attribute_id="path",
            display_name="Mount Path",
            placeholder="/mount/path",
            input_type="string",
            hidden=False,
            required=True,
            use_in_key=True,
        ),
        ListItemPropertyAttribute(
            attribute_id="pvc_name",
            display_name="Persistent Volume Claim Name",
            placeholder="pvc-name",
            input_type="string",
            hidden=False,
            required=True,
            use_in_key=False,
        ),
        ListItemPropertyAttribute(
            attribute_id="sub_path",
            display_name="Sub Path",
            placeholder="relative/path/within/volume",
            input_type="string",
            hidden=False,
            required=False,
            use_in_key=False,
        ),
        ListItemPropertyAttribute(
            attribute_id="read_only",
            display_name="Mount volume read-only",
            placeholder=None,
            default_value=False,
            input_type="boolean",
            hidden=False,
            required=False,
            use_in_key=False,
        ),
    ]

    def __init__(self, path: str, pvc_name: str, sub_path: str, read_only: bool, **kwargs):
        self.path = path
        self.pvc_name = pvc_name
        self.sub_path = sub_path
        self.read_only = read_only

    def get_all_validation_errors(self) -> List[str]:
        """Identify configuration issues for this instance"""
        validation_errors = []
        if not self.path:
            validation_errors.append("Required mount path was not specified.")
        if not self.pvc_name:
            validation_errors.append("Required persistent volume claim name was not specified.")
        elif not is_valid_kubernetes_resource_name(self.pvc_name):
            validation_errors.append(f"PVC name '{self.pvc_name}' is not a valid Kubernetes resource name.")
        if self.sub_path and self.sub_path.startswith("/"):
            validation_errors.append(f"Sub-path '{self.sub_path}' must be a relative path.")

        return validation_errors

    def add_to_execution_object(self, runtime_processor: RuntimePipelineProcessor, execution_object: Any, **kwargs):
        """Add VolumeMount instance to the execution object for the given runtime processor"""
        self.path = f"/{self.path.strip('/')}"  # normalize path
        if self.read_only is None:
            self.read_only = False
        runtime_processor.add_mounted_volume(instance=self, execution_object=execution_object, **kwargs)

    def should_discard(self) -> bool:
        """
        Returns a boolean indicating whether this VolumeMount instance should be silently discarded on
        the basis of its mount path, PVC name, and sub-path attribute values. If these attributes
        don't contain values this instance will not be validated or processed.
        """
        # ignore the read_only attribute because it always contains a value
        return not (self.path or self.pvc_name or self.sub_path)


class KubernetesAnnotation(ElyraPropertyListItem):
    """An ElyraProperty representing a single Kubernetes annotation"""

    applies_to_generic = True
    applies_to_custom = True

    property_id = KUBERNETES_POD_ANNOTATIONS
    property_display_name = "Kubernetes Pod Annotations"
    property_description = """Metadata to be added to this node. The metadata is exposed
    as annotation in the Kubernetes pod that executes this node."""
    property_attributes = [
        ListItemPropertyAttribute(
            attribute_id="key",
            display_name="Key",
            placeholder="annotation_key",
            input_type="string",
            hidden=False,
            required=True,
            use_in_key=True,
        ),
        ListItemPropertyAttribute(
            attribute_id="value",
            display_name="Value",
            placeholder="annotation_value",
            input_type="string",
            hidden=False,
            required=False,
            use_in_key=False,
        ),
    ]

    def __init__(self, key, value, **kwargs):
        self.key = key
        self.value = value

    def get_value_for_dict_entry(self) -> str:
        """Returns the value to be used when constructing a dict from a list of classes."""
        return self.value

    def get_all_validation_errors(self) -> List[str]:
        """Perform custom validation on an instance."""
        validation_errors = []
        # verify annotation key
        if not self.key:
            validation_errors.append("Required annotation key was not specified.")
        elif not is_valid_annotation_key(self.key):
            validation_errors.append(f"'{self.key}' is not a valid Kubernetes annotation key.")
        # verify annotation value
        if not is_valid_annotation_value(self.value):
            validation_errors.append(f"'{self.value}' is not a valid Kubernetes annotation value.")

        return validation_errors

    def add_to_execution_object(self, runtime_processor: RuntimePipelineProcessor, execution_object: Any, **kwargs):
        """Add KubernetesAnnotation instance to the execution object for the given runtime processor"""
        runtime_processor.add_kubernetes_pod_annotation(instance=self, execution_object=execution_object, **kwargs)


class KubernetesLabel(ElyraPropertyListItem):
    """An ElyraProperty representing a single Kubernetes pod label"""

    applies_to_generic = True
    applies_to_custom = True

    property_id = KUBERNETES_POD_LABELS
    property_display_name = "Kubernetes Pod Labels"
    property_description = """Metadata to be added to this node. The metadata is
    exposed as label in the Kubernetes pod that executes this node."""
    property_attributes = [
        ListItemPropertyAttribute(
            attribute_id="key",
            display_name="Key",
            placeholder="label_key",
            input_type="string",
            hidden=False,
            required=True,
            use_in_key=True,
        ),
        ListItemPropertyAttribute(
            attribute_id="value",
            display_name="Value",
            placeholder="label_value",
            input_type="string",
            hidden=False,
            required=False,
            use_in_key=False,
        ),
    ]

    def __init__(self, key, value, **kwargs):
        self.key = key
        self.value = value

    def get_value_for_dict_entry(self) -> str:
        """Returns the value to be used when constructing a dict from a list of classes."""
        return self.value

    def get_all_validation_errors(self) -> List[str]:
        """Perform custom validation on an instance."""
        validation_errors = []
        # verify label key
        if not self.key:
            validation_errors.append("Required label key was not specified.")
        elif not is_valid_label_key(self.key):
            validation_errors.append(f"'{self.key}' is not a valid Kubernetes label key.")
        # verify label value
        if not is_valid_label_value(self.value):
            validation_errors.append(f"'{self.value}' is not a valid Kubernetes label value.")
        return validation_errors

    def add_to_execution_object(self, runtime_processor: RuntimePipelineProcessor, execution_object: Any, **kwargs):
        """Add KubernetesLabel instance to the execution object for the given runtime processor"""
        runtime_processor.add_kubernetes_pod_label(instance=self, execution_object=execution_object, **kwargs)


class KubernetesToleration(ElyraPropertyListItem):
    """An ElyraProperty representing a single Kubernetes toleration"""

    applies_to_generic = True
    applies_to_custom = True

    property_id = KUBERNETES_TOLERATIONS
    property_display_name = "Kubernetes Tolerations"
    property_description = "Kubernetes tolerations to apply to the pod where the node is executed."
    property_attributes = [
        ListItemPropertyAttribute(
            attribute_id="key",
            display_name="Key",
            placeholder="key",
            input_type="string",
            hidden=False,
            required=False,
            use_in_key=True,
        ),
        ListItemPropertyAttribute(
            attribute_id="operator",
            display_name="Operator",
            input_type="string",
            default_value="Equal",
            enum=["Equal", "Exists"],
            hidden=False,
            required=True,
            use_in_key=True,
        ),
        ListItemPropertyAttribute(
            attribute_id="value",
            display_name="Value",
            placeholder="value",
            input_type="string",
            hidden=False,
            required=False,
            use_in_key=True,
        ),
        ListItemPropertyAttribute(
            attribute_id="effect",
            display_name="Effect",
            placeholder="NoSchedule",
            input_type="string",
            enum=["", "NoExecute", "NoSchedule", "PreferNoSchedule"],
            hidden=False,
            required=False,
            use_in_key=True,
        ),
    ]

    def __init__(self, key, operator, value, effect, **kwargs):
        self.key = key
        self.operator = operator
        self.value = value
        self.effect = effect

    def get_all_validation_errors(self) -> List[str]:
        """
        Perform custom validation on an instance using the constraints documented in
        https://kubernetes.io/docs/concepts/scheduling-eviction/taint-and-toleration/
        """
        validation_errors = []

        # Ensure the operator is valid
        if self.operator not in ["Exists", "Equal"]:
            validation_errors.append(
                f"'{self.operator}' is not a valid operator: the value must be one of 'Exists' or 'Equal'."
            )

        if self.operator == "Equal" and not self.key:
            validation_errors.append(
                f"'{self.operator}' is not a valid operator: operator must be 'Exists' if no key is specified."
            )

        if (
            self.effect is not None
            and len(self.effect) > 0
            and self.effect not in ["NoExecute", "NoSchedule", "PreferNoSchedule"]
        ):
            validation_errors.append(
                f"'{self.effect}' is not a valid effect: effect must be one "
                f"of 'NoExecute', 'NoSchedule', or 'PreferNoSchedule'."
            )

        if self.operator == "Exists" and self.value:
            validation_errors.append(
                f"'{self.value}' is not a valid value: value should be empty if operator is 'Exists'."
            )
        return validation_errors

    def add_to_execution_object(self, runtime_processor: RuntimePipelineProcessor, execution_object: Any, **kwargs):
        """Add KubernetesToleration instance to the execution object for the given runtime processor"""
        runtime_processor.add_kubernetes_toleration(instance=self, execution_object=execution_object, **kwargs)


class ElyraPropertyList(list):
    """
    A list class that exposes functionality specific to lists whose entries are
    of the class ElyraOwnedPropertyListItem.
    """

    def to_dict(self: List[ElyraPropertyListItem], use_prop_as_value: bool = False) -> Dict[str, str]:
        """
        Each Elyra-owned property consists of a set of attributes, some subset of which represents
        a unique key. Lists of these properties, however, often need converted to dictionary
        form for processing - so we must convert.
        """
        prop_dict = {}
        for prop in self:
            if prop is None or not isinstance(prop, ElyraPropertyListItem):
                continue  # invalid entry; skip inclusion and continue
            prop_key = prop.get_key_for_dict_entry()
            if prop_key is None:
                continue  # invalid entry; skip inclusion and continue

            prop_value = prop.get_value_for_dict_entry()
            if use_prop_as_value:
                prop_value = prop  # use of the property object itself as the value
            prop_dict[prop_key] = prop_value

        return prop_dict

    def deduplicate(self: ElyraPropertyList) -> ElyraPropertyList:
        """Remove duplicates from the given list"""
        instance_dict = self.to_dict(use_prop_as_value=True)
        return ElyraPropertyList({**instance_dict}.values())

    @staticmethod
    def merge(primary: ElyraPropertyList, secondary: ElyraPropertyList) -> ElyraPropertyList:
        """
        Merge two lists of Elyra-owned properties, preferring the values given in the
        primary parameter in the case of a matching key between the two lists.
        """
        primary_dict = primary.to_dict(use_prop_as_value=True)
        secondary_dict = secondary.to_dict(use_prop_as_value=True)

        merged_list = list({**secondary_dict, **primary_dict}.values())
        return ElyraPropertyList(merged_list)

    @staticmethod
    def difference(minuend: ElyraPropertyList, subtrahend: ElyraPropertyList) -> ElyraPropertyList:
        """
        Given two lists of Elyra-owned properties, remove any duplicate instances
        found in the second (subtrahend) from the first (minuend), if present.

        :param minuend: list to be subtracted from
        :param subtrahend: list from which duplicates will be determined and given preference

        :returns: the difference of the two lists
        """
        subtract_dict = minuend.to_dict(use_prop_as_value=True)
        for key in subtrahend.to_dict().keys():
            if key in subtract_dict:
                subtract_dict.pop(key)

        return ElyraPropertyList(subtract_dict.values())

    def add_to_execution_object(self, runtime_processor: RuntimePipelineProcessor, execution_object: Any):
        """
        Add a property instance to the execution object for the given runtime processor
        for each list item.
        """
        for item in self:
            if isinstance(item, ElyraPropertyListItem):
                item.add_to_execution_object(runtime_processor=runtime_processor, execution_object=execution_object)


class ElyraPropertyJSONEncoder(json.JSONEncoder):
    """
    A JSON Encoder class to prevent errors during serialization of Elyra-owned property classes.
    """

    def default(self, o):
        """
        Render dataclass content as dict
        """
        return o.__dict__ if isinstance(o, ElyraProperty) else super().default(o)


class ComponentParameter(object):
    """
    Represents a single property for a pipeline component
    """

    def __init__(
        self,
        id: str,
        name: str,
        json_data_type: str,
        description: str,
        value: Optional[Any] = "",
        allowed_input_types: Optional[List[Optional[str]]] = None,
        required: Optional[bool] = False,
        allow_no_options: Optional[bool] = False,
        items: Optional[List[str]] = None,
    ):
        """
        :param id: Unique identifier for a property
        :param name: The name of the property for display
        :param json_data_type: The JSON data type that represents this parameters value
        :param allowed_input_types: The input types that the property can accept, including those for custom rendering
        :param value: The default value of the property
        :param description: A description of the property for display
        :param required: Whether the property is required
        :param allow_no_options: Specifies whether to allow parent nodes that don't specifically
            define output properties to be selected as input to this node parameter
        :param items: For properties with a control of 'EnumControl', the items making up the enum
        """

        if not id:
            raise ValueError("Invalid component: Missing field 'id'.")
        if not name:
            raise ValueError("Invalid component: Missing field 'name'.")

        self._ref = id
        self._name = name
        self._json_data_type = json_data_type

        # The JSON type that the value entered for this property will be rendered in.
        # E.g., array types are entered by users and processed by the backend as
        # strings whereas boolean types are entered and processed as booleans
        self._value_entry_type = json_data_type
        if json_data_type in {"array", "object"}:
            self._value_entry_type = "string"

        if json_data_type == "boolean" and isinstance(value, str):
            value = value in ["True", "true"]
        elif json_data_type == "number" and isinstance(value, str):
            try:
                # Attempt to coerce string to integer value
                value = int(value)
            except ValueError:
                # Value could not be coerced to integer, assume float
                value = float(value)
        if json_data_type in {"array", "object"} and not isinstance(value, str):
            value = str(value)
        self._value = value

        self._description = description

        if not allowed_input_types:
            allowed_input_types = ["inputvalue", "inputpath", "file"]
        self._allowed_input_types = allowed_input_types

        self._items = items or []

        # Check description for information about 'required' parameter
        if "not optional" in description.lower() or (
            "required" in description.lower()
            and "not required" not in description.lower()
            and "n't required" not in description.lower()
        ):
            required = True

        self._required = required
        self._allow_no_options = allow_no_options

    @property
    def ref(self) -> str:
        return self._ref

    @property
    def name(self) -> str:
        return self._name

    @property
    def allowed_input_types(self) -> List[Optional[str]]:
        return self._allowed_input_types

    @property
    def json_data_type(self) -> str:
        return self._json_data_type

    @property
    def value_entry_type(self) -> str:
        return self._value_entry_type

    @property
    def value(self) -> Any:
        return self._value

    @property
    def description(self) -> str:
        return self._description

    @property
    def items(self) -> List[str]:
        return self._items

    @property
    def required(self) -> bool:
        return bool(self._required)

    @property
    def allow_no_options(self) -> bool:
        return self._allow_no_options

    @staticmethod
    def render_parameter_details(param: ComponentParameter) -> str:
        """
        Render the parameter data type and UI hints needed for the specified param for
        use in the custom component properties DAG template
        :returns: a string literal containing the JSON object to be rendered in the DAG
        """
        json_dict = {"title": param.name, "description": param.description}
        if len(param.allowed_input_types) == 1:
            # Parameter only accepts a single type of input
            input_type = param.allowed_input_types[0]
            if not input_type:
                # This is an output
                json_dict["type"] = "string"
                json_dict["uihints"] = {"ui:widget": "hidden", "outputpath": True}
            elif input_type == "inputpath":
                json_dict.update(
                    {
                        "type": "object",
                        "properties": {
                            "widget": {"type": "string", "default": input_type},
                            "value": {"type": "string", "enum": []},
                        },
                        "uihints": {"widget": {"ui:field": "hidden"}, "value": {input_type: True}},
                    }
                )
            elif input_type == "file":
                json_dict["type"] = "string"
                json_dict["uihints"] = {"ui:widget": input_type}
            else:
                json_dict["type"] = param.value_entry_type

                # Render default value if it is not None
                if param.value is not None:
                    json_dict["default"] = param.value
        else:
            # Parameter accepts multiple types of inputs; render a oneOf block
            one_of = []
            for widget_type in param.allowed_input_types:
                obj = {
                    "type": "object",
                    "properties": {"widget": {"type": "string"}, "value": {}},
                    "uihints": {"widget": {"ui:widget": "hidden"}, "value": {}},
                }
                if widget_type == "inputvalue":
                    obj["title"] = InputTypeDescriptionMap[param.value_entry_type].value
                    obj["properties"]["widget"]["default"] = param.value_entry_type
                    obj["properties"]["value"]["type"] = param.value_entry_type
                    if param.value_entry_type == "boolean":
                        obj["properties"]["value"]["title"] = " "

                    # Render default value if it is not None
                    if param.value is not None:
                        obj["properties"]["value"]["default"] = param.value
                else:  # inputpath or file types
                    obj["title"] = InputTypeDescriptionMap[widget_type].value
                    obj["properties"]["widget"]["default"] = widget_type
                    if widget_type == "outputpath":
                        obj["uihints"]["value"] = {"ui:readonly": "true", widget_type: True}
                        obj["properties"]["value"]["type"] = "string"
                    elif widget_type == "inputpath":
                        obj["uihints"]["value"] = {widget_type: True}
                        obj["properties"]["value"]["type"] = "string"
                        obj["properties"]["value"]["enum"] = []
                        if param.allow_no_options:
                            obj["uihints"]["allownooptions"] = param.allow_no_options
                    else:
                        obj["uihints"]["value"] = {"ui:widget": widget_type}
                        obj["properties"]["value"]["type"] = "string"

                one_of.append(obj)
            json_dict["oneOf"] = one_of

        return json.dumps(json_dict)


class InputTypeDescriptionMap(Enum):
    """A mapping of input types to the description that will appear in the UI"""

    string = "Please enter a string value:"
    number = "Please enter a number value:"
    boolean = "Please select or deselect the checkbox:"
    file = "Please select a file to use as input:"
    inputpath = "Please select an output from a parent:"
    outputpath = None  # outputs are read-only and don't require a description
