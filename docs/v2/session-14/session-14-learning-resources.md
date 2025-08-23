# Session 14: Learning Resources & Content Management System Implementation

## Objective
Implement a comprehensive learning resources and content management system that enables the creation, organization, and discovery of educational content. Focus on user-friendly content creation tools, efficient storage systems, and seamless integration with the existing community platform.

## Prerequisites
- Completed Session 10 (API completion and infrastructure)
- Understanding of the content management requirements
- Knowledge of file storage and media handling
- Familiarity with educational content workflows

## Task 1: Content Management Service Layer

### web/services/content_service.py

Create the content management business logic service:

```python
from typing import Optional, Dict, Any, List
from datetime import datetime
import structlog
from pathlib import Path

from web.config import WebConfig
from web.models.content import Content, ContentVersion, LearningPath
from web.crud.content import content_crud
from shared.exceptions import ContentError, ValidationError
from shared.types import ContentType, ContentStatus, DifficultyLevel

logger = structlog.get_logger()

class ContentService:
    """Service for content management operations."""
    
    def __init__(self, db_session, storage_service):
        self.db = db_session
        self.storage = storage_service
        self._content_cache: Dict[str, Dict[str, Any]] = {}
    
    async def create_content(
        self,
        title: str,
        content_type: ContentType,
        author_id: str,
        content_data: Dict[str, Any],
        tags: List[str] = None,
        difficulty: DifficultyLevel = DifficultyLevel.BEGINNER
    ) -> Dict[str, Any]:
        """Create new learning content."""
        try:
            # Validate content data
            validated_data = await self._validate_content(content_data, content_type)
            
            # Create content record
            content = await content_crud.create(
                db=self.db,
                title=title,
                content_type=content_type,
                author_id=author_id,
                content_data=validated_data,
                tags=tags or [],
                difficulty=difficulty,
                status=ContentStatus.DRAFT
            )
            
            # Process media files if present
            if 'media_files' in content_data:
                await self._process_media_files(content.id, content_data['media_files'])
            
            logger.info(
                "Content created",
                content_id=content.id,
                title=title,
                author_id=author_id,
                content_type=content_type.value
            )
            
            return {
                "id": str(content.id),
                "title": content.title,
                "status": content.status.value,
                "created_at": content.created_at.isoformat()
            }
            
        except Exception as e:
            logger.error(
                "Failed to create content",
                title=title,
                author_id=author_id,
                error=str(e)
            )
            raise ContentError(f"Failed to create content: {str(e)}")
    
    async def update_content(
        self,
        content_id: str,
        updates: Dict[str, Any],
        author_id: str
    ) -> Dict[str, Any]:
        """Update existing content with versioning."""
        content = await content_crud.get(self.db, content_id)
        
        if not content:
            raise ContentError("Content not found")
        
        if content.author_id != author_id:
            raise ContentError("Unauthorized to edit this content")
        
        try:
            # Create version backup
            await self._create_content_version(content)
            
            # Update content
            updated_content = await content_crud.update(
                db=self.db,
                content_id=content_id,
                updates=updates
            )
            
            # Invalidate cache
            self._invalidate_content_cache(content_id)
            
            logger.info(
                "Content updated",
                content_id=content_id,
                author_id=author_id
            )
            
            return {
                "id": str(updated_content.id),
                "title": updated_content.title,
                "status": updated_content.status.value,
                "updated_at": updated_content.updated_at.isoformat()
            }
            
        except Exception as e:
            logger.error(
                "Failed to update content",
                content_id=content_id,
                error=str(e)
            )
            raise ContentError(f"Failed to update content: {str(e)}")
    
    async def publish_content(self, content_id: str, author_id: str) -> Dict[str, Any]:
        """Publish content after validation."""
        content = await content_crud.get(self.db, content_id)
        
        if not content:
            raise ContentError("Content not found")
        
        if content.author_id != author_id:
            raise ContentError("Unauthorized to publish this content")
        
        # Validate content is ready for publishing
        validation_result = await self._validate_for_publishing(content)
        if not validation_result.is_valid:
            raise ValidationError(f"Content validation failed: {validation_result.errors}")
        
        try:
            # Update status to published
            published_content = await content_crud.update(
                db=self.db,
                content_id=content_id,
                updates={
                    "status": ContentStatus.PUBLISHED,
                    "published_at": datetime.utcnow()
                }
            )
            
            # Award bytes to author
            await self._award_creation_bytes(author_id, content.content_type)
            
            logger.info(
                "Content published",
                content_id=content_id,
                author_id=author_id,
                title=content.title
            )
            
            return {
                "id": str(published_content.id),
                "title": published_content.title,
                "status": published_content.status.value,
                "published_at": published_content.published_at.isoformat()
            }
            
        except Exception as e:
            logger.error(
                "Failed to publish content",
                content_id=content_id,
                error=str(e)
            )
            raise ContentError(f"Failed to publish content: {str(e)}")
    
    async def search_content(
        self,
        query: str = None,
        tags: List[str] = None,
        content_type: ContentType = None,
        difficulty: DifficultyLevel = None,
        limit: int = 20,
        offset: int = 0
    ) -> Dict[str, Any]:
        """Search and filter content."""
        try:
            results = await content_crud.search(
                db=self.db,
                query=query,
                tags=tags,
                content_type=content_type,
                difficulty=difficulty,
                limit=limit,
                offset=offset
            )
            
            return {
                "results": [
                    {
                        "id": str(content.id),
                        "title": content.title,
                        "content_type": content.content_type.value,
                        "difficulty": content.difficulty.value,
                        "tags": content.tags,
                        "author_id": content.author_id,
                        "created_at": content.created_at.isoformat(),
                        "view_count": content.view_count
                    }
                    for content in results.items
                ],
                "total": results.total,
                "limit": limit,
                "offset": offset
            }
            
        except Exception as e:
            logger.error(
                "Content search failed",
                query=query,
                error=str(e)
            )
            raise ContentError(f"Content search failed: {str(e)}")
    
    async def _validate_content(
        self,
        content_data: Dict[str, Any],
        content_type: ContentType
    ) -> Dict[str, Any]:
        """Validate content data based on type."""
        validators = {
            ContentType.TUTORIAL: self._validate_tutorial,
            ContentType.ARTICLE: self._validate_article,
            ContentType.VIDEO: self._validate_video,
            ContentType.CODE_SNIPPET: self._validate_code_snippet,
            ContentType.FAQ: self._validate_faq
        }
        
        validator = validators.get(content_type)
        if not validator:
            raise ValidationError(f"No validator for content type: {content_type}")
        
        return await validator(content_data)
    
    async def _validate_tutorial(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Validate tutorial content structure."""
        required_fields = ['introduction', 'steps', 'conclusion']
        
        for field in required_fields:
            if field not in data:
                raise ValidationError(f"Tutorial missing required field: {field}")
        
        # Validate steps structure
        if not isinstance(data['steps'], list) or len(data['steps']) < 1:
            raise ValidationError("Tutorial must have at least one step")
        
        for i, step in enumerate(data['steps']):
            if 'title' not in step or 'content' not in step:
                raise ValidationError(f"Step {i+1} missing title or content")
        
        return data
    
    async def _process_media_files(self, content_id: str, media_files: List[Dict]) -> None:
        """Process and store media files."""
        for media_file in media_files:
            # Upload to storage service
            stored_url = await self.storage.upload_file(
                file_data=media_file['data'],
                filename=media_file['filename'],
                content_type=media_file['content_type']
            )
            
            # Update content with media URL
            await content_crud.add_media(
                db=self.db,
                content_id=content_id,
                media_url=stored_url,
                media_type=media_file['content_type']
            )
```

## Related Files
- `web/models/content.py` - Content database models
- `web/crud/content.py` - Content CRUD operations
- `web/api/routes/content.py` - Content API endpoints
- `web/services/storage_service.py` - File storage service
- `shared/types.py` - Content type definitions

## Goals Achieved
- **Rich Content Creation**: Support for multiple content types and formats
- **Version Control**: Complete content versioning and history tracking
- **Content Validation**: Ensure content quality before publishing
- **Media Handling**: Comprehensive file upload and processing
- **Integration Ready**: Designed to work with existing platform features

## Dependencies
- Database models for content storage
- File storage service for media handling
- Search engine integration for content discovery
- Bytes economy integration for creator rewards
- Admin interface for content management

## Testing Strategy
```python
@pytest.mark.asyncio
async def test_create_tutorial_content(content_service, mock_db):
    """Test tutorial content creation."""
    tutorial_data = {
        "introduction": "Learn Python basics",
        "steps": [
            {"title": "Setup", "content": "Install Python"},
            {"title": "Hello World", "content": "Print hello world"}
        ],
        "conclusion": "You've learned Python basics"
    }
    
    result = await content_service.create_content(
        title="Python Basics",
        content_type=ContentType.TUTORIAL,
        author_id="user123",
        content_data=tutorial_data,
        tags=["python", "beginner"],
        difficulty=DifficultyLevel.BEGINNER
    )
    
    assert result["title"] == "Python Basics"
    assert result["status"] == "draft"
```

This learning resources system provides a comprehensive platform for creating, managing, and discovering educational content that integrates seamlessly with the existing community features.