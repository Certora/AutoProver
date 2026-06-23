#!/usr/bin/env python3
"""
Prompt Template Module

This module provides functionality for instantiating XML prompt templates
by replacing $VAR$ placeholders with actual values.
"""

import re
from pathlib import Path
from typing import Dict, Any


def _resolve_template_path(template_path: str) -> Path:
    """
    Resolve template path relative to the project root directory.

    Args:
        template_path: Relative path to template file (e.g., "prompts/template.xml")

    Returns:
        Absolute path to the template file
    """
    # Get the directory containing this script (src/utils/)
    script_dir = Path(__file__).parent
    # Go up to project root (src/utils/ -> src/ -> project_root/)
    project_root = script_dir.parent.parent
    # Resolve the template path relative to project root
    return project_root / template_path


class PromptTemplate:
    """Class for handling XML prompt template instantiation."""
    
    def __init__(self, template_path: str):
        """
        Initialize the PromptTemplate with an XML file path.

        Args:
            template_path: Relative path to the XML template file (e.g., "prompts/template.xml")
        """
        self.template_path = _resolve_template_path(template_path)
        self.template_content = None
        self._load_template()
    
    def _load_template(self):
        """Load the XML template content from file."""
        if not self.template_path.exists():
            raise FileNotFoundError(f"Template file not found: {self.template_path}")
        
        try:
            with open(self.template_path, 'r', encoding='utf-8') as f:
                self.template_content = f.read()
        except Exception as e:
            raise ValueError(f"Error reading template file {self.template_path}: {e}")
    
    def instantiate(self, variables: Dict[str, Any]) -> str:
        """
        Instantiate the template by replacing $VAR$ placeholders with actual values.
        
        Args:
            variables: Dictionary mapping variable names to their values
            
        Returns:
            The instantiated template content
            
        Raises:
            ValueError: If template content is not loaded or variables are missing
        """
        if self.template_content is None:
            raise ValueError("Template content not loaded")
        
        content = self.template_content
        
        # Find all $VAR$ patterns in the template
        pattern = r'\$([A-Z_][A-Z0-9_]*)\$'
        found_variables = set(re.findall(pattern, content))
        
        # Check that all required variables are provided
        missing_vars = found_variables - set(variables.keys())
        if missing_vars:
            raise ValueError(f"Missing variables for template instantiation: {missing_vars}")
        
        # Replace each $VAR$ with its value
        for var_name, value in variables.items():
            if var_name in found_variables:
                placeholder = f"${var_name}$"
                content = content.replace(placeholder, str(value))
        
        return content
    
    def get_required_variables(self) -> set:
        """
        Get the set of variable names required by this template.
        
        Returns:
            Set of variable names found in the template
        """
        if self.template_content is None:
            raise ValueError("Template content not loaded")
        
        pattern = r'\$([A-Z_][A-Z0-9_]*)\$'
        return set(re.findall(pattern, self.template_content))
    
    def extract_instructions(self) -> str:
        """
        Extract the INSTRUCTIONS section from the XML template.
        
        Returns:
            The content within <INSTRUCTIONS>...</INSTRUCTIONS> tags
        """
        if self.template_content is None:
            raise ValueError("Template content not loaded")
        
        # Extract instructions section
        instructions_pattern = r'<INSTRUCTIONS>(.*?)</INSTRUCTIONS>'
        match = re.search(instructions_pattern, self.template_content, re.DOTALL)
        
        if match:
            return match.group(1).strip()
        else:
            # If no INSTRUCTIONS section, return the whole content
            return self.template_content


def instantiate_xml_prompt(template_path: str, variables: Dict[str, Any]) -> str:
    """
    Convenience function to instantiate an XML prompt template.

    Args:
        template_path: Relative path to the XML template file (e.g., "prompts/template.xml")
        variables: Dictionary mapping variable names to their values

    Returns:
        The instantiated prompt content
    """
    template = PromptTemplate(template_path)
    return template.instantiate(variables)


def get_prompt_instructions(template_path: str, variables: Dict[str, Any]) -> str:
    """
    Convenience function to get the instantiated instructions from an XML prompt template.

    Args:
        template_path: Relative path to the XML template file (e.g., "prompts/template.xml")
        variables: Dictionary mapping variable names to their values

    Returns:
        The instantiated instructions content
    """
    template = PromptTemplate(template_path)
    instantiated_content = template.instantiate(variables)
    
    # Extract instructions from the instantiated content
    instructions_pattern = r'<INSTRUCTIONS>(.*?)</INSTRUCTIONS>'
    match = re.search(instructions_pattern, instantiated_content, re.DOTALL)
    
    if match:
        return match.group(1).strip()
    else:
        # If no INSTRUCTIONS section, return the whole content
        return instantiated_content