# Session 14.1: Content Creation System - Rich Content Authoring

## Overview
Implement a comprehensive content creation system with rich text editing, code snippet management, and multi-format content support. Focus on user-friendly authoring tools and robust content validation.

## Key Components
- Rich text editor with markdown support
- Code snippet management with syntax highlighting
- Multi-format content support (text, images, videos)
- Draft and publishing workflows
- Content validation and preprocessing

## Implementation Details

### Rich Text Editor Integration
Modern content editing interface:

```typescript
// Frontend: Rich text editor component
import { Editor } from '@tiptap/react'
import { StarterKit } from '@tiptap/starter-kit'
import { CodeBlockLowlight } from '@tiptap/extension-code-block-lowlight'
import { Image } from '@tiptap/extension-image'
import { Link } from '@tiptap/extension-link'

interface ContentEditorProps {
  content: string
  onChange: (content: string) => void
  onSave: () => void
}

export const ContentEditor: React.FC<ContentEditorProps> = ({
  content,
  onChange,
  onSave
}) => {
  const editor = useEditor({
    extensions: [
      StarterKit,
      CodeBlockLowlight,
      Image.configure({
        HTMLAttributes: {
          class: 'content-image',
        },
      }),
      Link.configure({
        openOnClick: false,
      }),
    ],
    content,
    onUpdate: ({ editor }) => {
      onChange(editor.getHTML())
    },
  })

  return (
    <div className="content-editor">
      <div className="editor-toolbar">
        <ToolbarButton 
          onClick={() => editor?.chain().focus().toggleBold().run()}
          active={editor?.isActive('bold')}
        >
          Bold
        </ToolbarButton>
        <ToolbarButton 
          onClick={() => editor?.chain().focus().toggleItalic().run()}
          active={editor?.isActive('italic')}
        >
          Italic
        </ToolbarButton>
        <ToolbarButton 
          onClick={() => editor?.chain().focus().toggleCodeBlock().run()}
          active={editor?.isActive('codeBlock')}
        >
          Code Block
        </ToolbarButton>
        <SaveButton onClick={onSave}>
          Save Draft
        </SaveButton>
      </div>
      <EditorContent editor={editor} className="editor-content" />
    </div>
  )
}
```

### Code Snippet Management
Specialized handling for code content:

```python
from pygments import highlight
from pygments.lexers import get_lexer_by_name
from pygments.formatters import HtmlFormatter
from typing import Dict, Any, List

class CodeSnippetProcessor:
    """Process and validate code snippets."""
    
    SUPPORTED_LANGUAGES = [
        'python', 'javascript', 'typescript', 'java', 'cpp', 
        'c', 'rust', 'go', 'sql', 'html', 'css', 'bash'
    ]
    
    def __init__(self):
        self.formatter = HtmlFormatter(style='github', linenos=True)
    
    def process_code_snippet(self, code: str, language: str) -> Dict[str, Any]:
        """Process code snippet with syntax highlighting."""
        if language not in self.SUPPORTED_LANGUAGES:
            raise ValueError(f"Unsupported language: {language}")
        
        try:
            lexer = get_lexer_by_name(language)
            highlighted_code = highlight(code, lexer, self.formatter)
            
            # Validate code syntax (basic check)
            validation_result = self._validate_code_syntax(code, language)
            
            return {
                "original_code": code,
                "highlighted_html": highlighted_code,
                "language": language,
                "line_count": code.count('\n') + 1,
                "validation": validation_result
            }
            
        except Exception as e:
            return {
                "original_code": code,
                "highlighted_html": f"<pre><code>{code}</code></pre>",
                "language": language,
                "error": str(e)
            }
    
    def _validate_code_syntax(self, code: str, language: str) -> Dict[str, Any]:
        """Basic syntax validation for code snippets."""
        validation = {
            "is_valid": True,
            "warnings": [],
            "suggestions": []
        }
        
        # Language-specific validation
        if language == 'python':
            validation.update(self._validate_python_syntax(code))
        elif language == 'javascript':
            validation.update(self._validate_javascript_syntax(code))
        
        return validation
    
    def _validate_python_syntax(self, code: str) -> Dict[str, Any]:
        """Validate Python code syntax."""
        try:
            compile(code, '<string>', 'exec')
            return {"is_valid": True, "syntax_error": None}
        except SyntaxError as e:
            return {
                "is_valid": False,
                "syntax_error": str(e),
                "line_number": e.lineno
            }
```

### Content Type Handlers
Specialized handlers for different content types:

```python
from abc import ABC, abstractmethod
from typing import Dict, Any, List
from dataclasses import dataclass

@dataclass
class ValidationResult:
    is_valid: bool
    errors: List[str]
    warnings: List[str]
    suggestions: List[str]

class ContentTypeHandler(ABC):
    """Base class for content type handlers."""
    
    @abstractmethod
    async def validate(self, content_data: Dict[str, Any]) -> ValidationResult:
        """Validate content structure and data."""
        pass
    
    @abstractmethod
    async def process(self, content_data: Dict[str, Any]) -> Dict[str, Any]:
        """Process content for storage."""
        pass

class TutorialHandler(ContentTypeHandler):
    """Handler for tutorial content."""
    
    async def validate(self, content_data: Dict[str, Any]) -> ValidationResult:
        """Validate tutorial structure."""
        errors = []
        warnings = []
        suggestions = []
        
        # Required fields validation
        required_fields = ['title', 'introduction', 'steps', 'conclusion']
        for field in required_fields:
            if field not in content_data or not content_data[field]:
                errors.append(f"Missing required field: {field}")
        
        # Steps validation
        if 'steps' in content_data:
            steps = content_data['steps']
            if not isinstance(steps, list):
                errors.append("Steps must be a list")
            elif len(steps) < 2:
                warnings.append("Tutorials with fewer than 2 steps may not be effective")
            
            for i, step in enumerate(steps):
                if not isinstance(step, dict):
                    errors.append(f"Step {i+1} must be an object")
                    continue
                
                if 'title' not in step or not step['title']:
                    errors.append(f"Step {i+1} missing title")
                
                if 'content' not in step or not step['content']:
                    errors.append(f"Step {i+1} missing content")
                
                # Suggest improvements
                if len(step.get('content', '')) < 50:
                    suggestions.append(f"Step {i+1} content seems brief, consider adding more detail")
        
        # Content length validation
        if 'introduction' in content_data:
            intro_length = len(content_data['introduction'])
            if intro_length < 100:
                warnings.append("Introduction seems brief, consider expanding to better engage readers")
            elif intro_length > 1000:
                warnings.append("Introduction is quite long, consider making it more concise")
        
        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            suggestions=suggestions
        )
    
    async def process(self, content_data: Dict[str, Any]) -> Dict[str, Any]:
        """Process tutorial content."""
        processed_data = content_data.copy()
        
        # Generate estimated reading time
        total_words = self._count_words(content_data)
        estimated_time = max(1, total_words // 200)  # 200 words per minute
        processed_data['estimated_reading_time'] = f"{estimated_time} min"
        
        # Generate table of contents
        if 'steps' in content_data:
            toc = [
                {"title": step.get('title', f"Step {i+1}"), "anchor": f"step-{i+1}"}
                for i, step in enumerate(content_data['steps'])
            ]
            processed_data['table_of_contents'] = toc
        
        # Process code blocks in content
        processed_data = await self._process_code_blocks(processed_data)
        
        return processed_data
    
    def _count_words(self, content_data: Dict[str, Any]) -> int:
        """Count total words in tutorial content."""
        word_count = 0
        
        # Count words in introduction and conclusion
        for field in ['introduction', 'conclusion']:
            if field in content_data:
                word_count += len(content_data[field].split())
        
        # Count words in steps
        if 'steps' in content_data:
            for step in content_data['steps']:
                if 'content' in step:
                    word_count += len(step['content'].split())
        
        return word_count

class ArticleHandler(ContentTypeHandler):
    """Handler for article content."""
    
    async def validate(self, content_data: Dict[str, Any]) -> ValidationResult:
        """Validate article structure."""
        errors = []
        warnings = []
        suggestions = []
        
        # Required fields
        if 'title' not in content_data or not content_data['title']:
            errors.append("Article title is required")
        
        if 'content' not in content_data or not content_data['content']:
            errors.append("Article content is required")
        
        # Content length validation
        if 'content' in content_data:
            content_length = len(content_data['content'])
            if content_length < 300:
                warnings.append("Article seems brief, consider adding more content")
            elif content_length > 10000:
                warnings.append("Article is quite long, consider breaking into sections")
        
        # Check for headings structure
        if 'content' in content_data:
            content = content_data['content']
            if not any(line.startswith('#') for line in content.split('\n')):
                suggestions.append("Consider adding headings to improve article structure")
        
        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            suggestions=suggestions
        )
    
    async def process(self, content_data: Dict[str, Any]) -> Dict[str, Any]:
        """Process article content."""
        processed_data = content_data.copy()
        
        # Generate reading time
        word_count = len(content_data.get('content', '').split())
        reading_time = max(1, word_count // 200)
        processed_data['estimated_reading_time'] = f"{reading_time} min"
        
        # Extract and process headings for TOC
        if 'content' in content_data:
            headings = self._extract_headings(content_data['content'])
            if headings:
                processed_data['table_of_contents'] = headings
        
        return processed_data
    
    def _extract_headings(self, content: str) -> List[Dict[str, Any]]:
        """Extract headings from markdown content."""
        headings = []
        for line in content.split('\n'):
            line = line.strip()
            if line.startswith('#'):
                level = len(line) - len(line.lstrip('#'))
                title = line.lstrip('#').strip()
                anchor = title.lower().replace(' ', '-').replace('[^a-z0-9-]', '')
                headings.append({
                    "level": level,
                    "title": title,
                    "anchor": anchor
                })
        return headings
```

### Content Upload API
Backend API for content creation:

```python
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from typing import List, Optional
from pydantic import BaseModel

router = APIRouter(prefix="/content", tags=["content"])

class CreateContentRequest(BaseModel):
    title: str
    content_type: str
    content_data: dict
    tags: Optional[List[str]] = []
    difficulty: Optional[str] = "beginner"

@router.post("/create", response_model=ContentResponse)
async def create_content(
    request: CreateContentRequest,
    user: CurrentUser = Depends(),
    content_service: ContentService = Depends(),
    db: DatabaseSession = Depends()
):
    """Create new learning content."""
    try:
        result = await content_service.create_content(
            title=request.title,
            content_type=ContentType(request.content_type),
            author_id=str(user.id),
            content_data=request.content_data,
            tags=request.tags,
            difficulty=DifficultyLevel(request.difficulty)
        )
        
        return ContentResponse(**result)
        
    except ValidationError as e:
        raise HTTPException(400, detail=str(e))
    except ContentError as e:
        raise HTTPException(500, detail=str(e))

@router.post("/upload-media")
async def upload_media(
    files: List[UploadFile] = File(...),
    user: CurrentUser = Depends(),
    storage_service: StorageService = Depends()
):
    """Upload media files for content."""
    uploaded_files = []
    
    for file in files:
        # Validate file type
        if not file.content_type.startswith(('image/', 'video/', 'audio/')):
            raise HTTPException(400, f"Unsupported file type: {file.content_type}")
        
        # Upload to storage
        file_url = await storage_service.upload_file(
            file_data=await file.read(),
            filename=file.filename,
            content_type=file.content_type,
            folder="content-media"
        )
        
        uploaded_files.append({
            "filename": file.filename,
            "url": file_url,
            "content_type": file.content_type
        })
    
    return {"uploaded_files": uploaded_files}

@router.post("/{content_id}/publish")
async def publish_content(
    content_id: str,
    user: CurrentUser = Depends(),
    content_service: ContentService = Depends()
):
    """Publish content after validation."""
    try:
        result = await content_service.publish_content(content_id, str(user.id))
        return result
        
    except ContentError as e:
        raise HTTPException(400, detail=str(e))
    except ValidationError as e:
        raise HTTPException(400, detail=str(e))
```

## Related Files
- `web/services/content_service.py` - Content management service
- `web/api/routes/content.py` - Content API endpoints
- `web/models/content.py` - Content database models
- `frontend/components/ContentEditor.tsx` - Rich text editor component
- `web/services/storage_service.py` - File storage service

## Goals Achieved
- **Rich Content Creation**: Modern editing interface with full formatting support
- **Code Integration**: Specialized handling for code snippets with syntax highlighting
- **Multi-Format Support**: Handle text, images, videos, and interactive content
- **Validation Pipeline**: Comprehensive content validation before publishing
- **User-Friendly Workflow**: Intuitive draft and publish workflow

## Dependencies
- Rich text editor library (TipTap/Editor.js)
- Code syntax highlighting (Pygments)
- File storage service for media uploads
- Content validation framework
- Database models for content storage

## Testing Strategy
```python
@pytest.mark.asyncio
async def test_tutorial_validation():
    """Test tutorial content validation."""
    handler = TutorialHandler()
    
    valid_tutorial = {
        "title": "Python Basics",
        "introduction": "Learn Python programming fundamentals",
        "steps": [
            {"title": "Setup", "content": "Install Python on your system"},
            {"title": "Hello World", "content": "Write your first Python program"}
        ],
        "conclusion": "You now know Python basics"
    }
    
    result = await handler.validate(valid_tutorial)
    assert result.is_valid == True
    assert len(result.errors) == 0
```

This content creation system provides authors with powerful tools to create engaging, well-structured learning content that integrates seamlessly with the platform's community features.