# Campaign Challenges Database Schema Plan

## Integration Analysis

Based on the existing codebase analysis, the campaign challenges system will integrate with:

### Existing Models
- `Squad` - For squad-based campaigns
- `SquadMembership` - For squad member validation
- `BytesBalance` - For costs and rewards integration
- `BytesTransaction` - For campaign entry fees/rewards

### Existing Infrastructure
- SQLAlchemy async ORM with PostgreSQL support
- Alembic migrations system
- Repository pattern in `/web/repositories/`
- Service pattern in `/web/services/`
- FastAPI routes in `/web/api/routes/`

## New Database Models

### 1. Campaign Model

```python
class Campaign(Base):
    """Campaign definition for challenge competitions."""
    
    __tablename__ = "campaigns"
    
    # Primary key
    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        doc="Unique campaign identifier"
    )
    
    # Guild context
    guild_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        index=True,
        doc="Discord guild (server) snowflake ID"
    )
    
    # Campaign metadata
    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc="Display name of the campaign"
    )
    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="Optional campaign description"
    )
    
    # Campaign type and settings
    campaign_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        doc="Type: 'player' or 'squad'"
    )
    state: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="draft",
        doc="State: 'draft', 'active', 'completed'"
    )
    
    # Timing configuration
    start_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="When the campaign starts"
    )
    release_delay_minutes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1440,  # 24 hours
        doc="Minutes between challenge releases"
    )
    
    # Scoring configuration
    scoring_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="time_based",
        doc="Scoring type: 'time_based' or 'point_based'"
    )
    starting_points: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        doc="Starting points for point-based scoring"
    )
    points_decrease_step: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        doc="Point decrease per position for point-based scoring"
    )
    
    # Discord integration
    announcement_channel_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        doc="Discord channel for campaign announcements"
    )
    
    # Indexes and constraints
    __table_args__ = (
        Index("ix_campaigns_guild_id", "guild_id"),
        Index("ix_campaigns_state", "state"),
        Index("ix_campaigns_start_date", "start_date"),
        Index("ix_campaigns_guild_state", "guild_id", "state"),
        CheckConstraint("campaign_type IN ('player', 'squad')", name="ck_campaigns_type"),
        CheckConstraint("state IN ('draft', 'active', 'completed')", name="ck_campaigns_state"),
        CheckConstraint("scoring_type IN ('time_based', 'point_based')", name="ck_campaigns_scoring_type"),
        CheckConstraint("release_delay_minutes > 0", name="ck_campaigns_release_delay_positive"),
    )
```

### 2. Challenge Model

```python
class Challenge(Base):
    """Individual challenge within a campaign."""
    
    __tablename__ = "challenges"
    
    # Primary key
    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        doc="Unique challenge identifier"
    )
    
    # Campaign relationship
    campaign_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Campaign this challenge belongs to"
    )
    
    # Challenge ordering and metadata
    order_position: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        doc="Order position within the campaign (1-based)"
    )
    title: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        doc="Challenge title"
    )
    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="Challenge description in Markdown"
    )
    
    # Generation script
    generation_script: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="Python script for generating inputs"
    )
    script_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        doc="When generation script was last updated"
    )
    
    # Private metadata (not shown to participants)
    categories: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        doc="Categories/tags for admin organization"
    )
    difficulty_level: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        doc="Difficulty level (1-10)"
    )
    
    # Indexes and constraints
    __table_args__ = (
        Index("ix_challenges_campaign_id", "campaign_id"),
        Index("ix_challenges_campaign_order", "campaign_id", "order_position"),
        Index("ix_challenges_script_updated", "script_updated_at"),
        UniqueConstraint("campaign_id", "order_position", name="uq_challenges_campaign_order"),
        CheckConstraint("order_position > 0", name="ck_challenges_order_positive"),
        CheckConstraint("difficulty_level >= 1 AND difficulty_level <= 10", name="ck_challenges_difficulty_range"),
    )
```

### 3. Generated Input Cache Model

```python
class GeneratedInputCache(Base):
    """Cache for generated challenge inputs per participant."""
    
    __tablename__ = "generated_input_cache"
    
    # Primary key
    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        doc="Unique cache entry identifier"
    )
    
    # Challenge relationship
    challenge_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("challenges.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Challenge this input was generated for"
    )
    
    # Participant identification
    participant_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        doc="Player ID or Squad ID depending on campaign type"
    )
    participant_type: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        doc="Type: 'player' or 'squad'"
    )
    
    # Generated data
    input_json: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        doc="Generated input data as JSON"
    )
    expected_result: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="Expected result for validation"
    )
    
    # Cache validity
    is_valid: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        index=True,
        doc="Whether this cache entry is still valid"
    )
    generation_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        doc="When this input was generated"
    )
    first_request_timestamp: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="When participant first requested this input"
    )
    
    # Indexes and constraints
    __table_args__ = (
        Index("ix_generated_input_cache_challenge_id", "challenge_id"),
        Index("ix_generated_input_cache_participant", "participant_id", "participant_type"),
        Index("ix_generated_input_cache_validity", "is_valid"),
        Index("ix_generated_input_cache_challenge_participant", "challenge_id", "participant_id", "participant_type"),
        UniqueConstraint("challenge_id", "participant_id", "participant_type", name="uq_generated_input_cache_challenge_participant"),
        CheckConstraint("participant_type IN ('player', 'squad')", name="ck_generated_input_cache_participant_type"),
    )
```

### 4. Submission Records Model

```python
class ChallengeSubmission(Base):
    """Records of challenge submissions by participants."""
    
    __tablename__ = "challenge_submissions"
    
    # Primary key
    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        doc="Unique submission identifier"
    )
    
    # Challenge relationship
    challenge_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("challenges.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Challenge this submission is for"
    )
    
    # Participant identification
    participant_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        doc="Player ID or Squad ID depending on campaign type"
    )
    participant_type: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        doc="Type: 'player' or 'squad'"
    )
    
    # Submission data
    submitted_result: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="Participant's submitted answer"
    )
    is_correct: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        doc="Whether the submission was correct"
    )
    points_awarded: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        doc="Points awarded for this submission"
    )
    
    # Timing data
    submission_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
        doc="When the submission was made"
    )
    
    # Indexes and constraints
    __table_args__ = (
        Index("ix_challenge_submissions_challenge_id", "challenge_id"),
        Index("ix_challenge_submissions_participant", "participant_id", "participant_type"),
        Index("ix_challenge_submissions_timestamp", "submission_timestamp"),
        Index("ix_challenge_submissions_challenge_participant", "challenge_id", "participant_id", "participant_type"),
        Index("ix_challenge_submissions_correct", "is_correct"),
        CheckConstraint("participant_type IN ('player', 'squad')", name="ck_challenge_submissions_participant_type"),
        CheckConstraint("points_awarded >= 0", name="ck_challenge_submissions_points_non_negative"),
    )
```

### 5. Rate Limiting Model

```python
class SubmissionRateLimit(Base):
    """Rate limiting tracking for challenge submissions."""
    
    __tablename__ = "submission_rate_limits"
    
    # Primary key
    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        doc="Unique rate limit entry identifier"
    )
    
    # Participant identification
    participant_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        index=True,
        doc="Player ID or Squad ID depending on campaign type"
    )
    participant_type: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        doc="Type: 'player' or 'squad'"
    )
    
    # Rate limit tracking
    submission_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
        doc="When the submission attempt was made"
    )
    
    # Indexes and constraints
    __table_args__ = (
        Index("ix_submission_rate_limits_participant", "participant_id", "participant_type"),
        Index("ix_submission_rate_limits_timestamp", "submission_timestamp"),
        Index("ix_submission_rate_limits_participant_timestamp", "participant_id", "participant_type", "submission_timestamp"),
        CheckConstraint("participant_type IN ('player', 'squad')", name="ck_submission_rate_limits_participant_type"),
    )
```

## Migration Strategy

### Phase 1: Core Models (Migration 1)
- Create Campaign, Challenge, GeneratedInputCache models
- Add basic indexes and constraints

### Phase 2: Submission Tracking (Migration 2)
- Create ChallengeSubmission, SubmissionRateLimit models
- Add foreign key relationships

### Phase 3: Indexes and Optimization (Migration 3)
- Add composite indexes for performance
- Add any missing constraints

### Phase 4: Integration Enhancements (Migration 4)
- Add any additional fields discovered during implementation
- Performance optimizations based on testing

## Repository Pattern Integration

Following existing pattern in `/web/repositories/`:

```python
# campaign_repository.py
class CampaignRepository(BaseRepository[Campaign])

# challenge_repository.py  
class ChallengeRepository(BaseRepository[Challenge])

# submission_repository.py
class SubmissionRepository(BaseRepository[ChallengeSubmission])
```

## Service Layer Integration

Following existing pattern in `/web/services/`:

```python
# campaign_service.py
class CampaignService

# challenge_service.py
class ChallengeService

# submission_service.py
class SubmissionService
```

## API Routes Integration

Following existing pattern in `/web/api/routes/`:

```python
# campaigns.py - Campaign CRUD
# challenges.py - Challenge management  
# submissions.py - Submission handling
# leaderboards.py - Leaderboard data
```

## Testing Strategy

Each model will have comprehensive tests in `/tests/web/test_models/`:
- Model creation and validation
- Constraint enforcement
- Relationship integrity
- Index performance

This schema design integrates seamlessly with the existing codebase while providing all functionality needed for the campaign challenges system.