# Session 7.2: Balance and Transfer Management - Bytes Economy System

## Overview
Implement comprehensive balance management and transfer system allowing users to check balances, send bytes to other users, and manage their bytes economy participation.

## Key Components
- Balance checking with daily eligibility detection
- User-to-user bytes transfers with validation
- Transfer confirmation for large amounts
- Balance history and transaction logging
- Guild configuration and limits management

## Implementation Details

### Balance Checking Service
Core service method for retrieving user balance and status:

```python
async def check_balance(
    self,
    guild_id: str,
    user_id: str,
    username: str
) -> Dict[str, Any]:
    """Check user balance and award daily if eligible."""
    try:
        # Get current balance
        balance_data = await self.api.get_bytes_balance(guild_id, user_id)
        
        # Check if daily is available
        last_daily = balance_data.get("last_daily")
        today = utctoday()
        daily_available = False
        
        if last_daily:
            last_daily_date = datetime.fromisoformat(last_daily).date()
            daily_available = last_daily_date < today
        else:
            daily_available = True
        
        return {
            **balance_data,
            "daily_available": daily_available
        }
        
    except Exception as e:
        logger.error(
            "Failed to check balance",
            guild_id=guild_id,
            user_id=user_id,
            error=str(e)
        )
        
        # Return new user data
        config = await self.get_config(guild_id)
        return {
            "balance": config["starting_balance"],
            "total_received": 0,
            "total_sent": 0,
            "streak_count": 0,
            "daily_available": True
        }
```

### Transfer System with Validation
Secure transfer system with comprehensive validation:

```python
async def transfer(
    self,
    guild_id: str,
    giver_id: str,
    giver_username: str,
    receiver_id: str,
    receiver_username: str,
    amount: int,
    reason: Optional[str] = None
) -> Dict[str, Any]:
    """Transfer bytes between users."""
    # Validation
    if giver_id == receiver_id:
        raise ValueError("Cannot transfer bytes to yourself!")
    
    if amount <= 0:
        raise ValueError("Amount must be positive!")
    
    # Get config
    config = await self.get_config(guild_id)
    max_transfer = config.get("max_transfer", 1000)
    
    if amount > max_transfer:
        raise ValueError(f"Maximum transfer amount is **{max_transfer:,}** bytes!")
    
    # Check balance
    giver_balance = await self.check_balance(guild_id, giver_id, giver_username)
    
    if giver_balance["balance"] < amount:
        raise InsufficientBytesError(
            current=giver_balance["balance"],
            required=amount
        )
    
    try:
        # Execute transfer
        result = await self.api.transfer_bytes(
            guild_id=guild_id,
            giver_id=giver_id,
            giver_username=giver_username,
            receiver_id=receiver_id,
            receiver_username=receiver_username,
            amount=amount,
            reason=reason
        )
        
        logger.info(
            "Bytes transferred",
            guild_id=guild_id,
            giver_id=giver_id,
            receiver_id=receiver_id,
            amount=amount
        )
        
        return result
        
    except Exception as e:
        logger.error(
            "Failed to transfer bytes",
            guild_id=guild_id,
            error=str(e)
        )
        raise
```

### Discord Commands Implementation
User-friendly commands for balance management:

```python
@bytes_group.child
@lightbulb.command("balance", "Check your bytes balance", aliases=["bal"])
@lightbulb.implements(lightbulb.SlashSubCommand)
@guild_only()
async def balance_command(ctx: lightbulb.SlashContext) -> None:
    """Check bytes balance."""
    await ctx.respond(hikari.ResponseType.DEFERRED)
    
    try:
        # Get balance
        balance_data = await bytes_plugin.service.check_balance(
            str(ctx.guild_id),
            str(ctx.author.id),
            ctx.author.username
        )
        
        # Create embed
        embed = EmbedBuilder.bytes_balance(
            user=ctx.author,
            balance=balance_data["balance"],
            total_received=balance_data["total_received"],
            total_sent=balance_data["total_sent"],
            streak=balance_data.get("streak_count", 0),
            daily_available=balance_data.get("daily_available", False)
        )
        
        await ctx.respond(embed=embed)
        
    except Exception as e:
        logger.error("Balance check failed", error=str(e))
        await bytes_plugin.send_error(
            ctx,
            "Failed to check balance. Please try again later."
        )
```

### Transfer Command with Confirmation
Transfer command with interactive confirmation for large amounts:

```python
@bytes_group.child
@lightbulb.option(
    "reason",
    "Reason for the transfer",
    type=str,
    required=False,
    max_length=100
)
@lightbulb.option(
    "amount",
    "Amount of bytes to send",
    type=int,
    required=True,
    min_value=1
)
@lightbulb.option(
    "user",
    "User to send bytes to",
    type=hikari.User,
    required=True
)
@lightbulb.command("send", "Send bytes to another user", aliases=["give", "transfer"])
@lightbulb.implements(lightbulb.SlashSubCommand)
@guild_only()
@cooldown(seconds=60, bucket=lightbulb.buckets.UserBucket)
async def send_command(ctx: lightbulb.SlashContext) -> None:
    """Send bytes to another user."""
    receiver = ctx.options.user
    amount = ctx.options.amount
    reason = ctx.options.reason
    
    # Check if receiver is a bot
    if receiver.is_bot:
        await bytes_plugin.send_error(ctx, "You cannot send bytes to bots!")
        return
    
    # For large amounts, require confirmation
    if amount >= 100:
        view = TransferConfirmView(
            giver=ctx.author,
            receiver=receiver,
            amount=amount,
            reason=reason,
            timeout=60
        )
        
        embed = EmbedBuilder.info(
            "Confirm Transfer",
            f"Send **{amount:,}** bytes to {receiver.mention}?",
            fields=[
                ("Reason", reason or "No reason provided", False)
            ]
        )
        
        resp = await ctx.respond(embed=embed, components=view)
        await view.start(await resp.message())
        await view.wait()
        
        if not view.confirmed:
            return
    else:
        await ctx.respond(hikari.ResponseType.DEFERRED)
    
    try:
        # Execute transfer
        result = await bytes_plugin.service.transfer(
            guild_id=str(ctx.guild_id),
            giver_id=str(ctx.author.id),
            giver_username=ctx.author.username,
            receiver_id=str(receiver.id),
            receiver_username=receiver.username,
            amount=amount,
            reason=reason
        )
        
        # Success embed
        embed = EmbedBuilder.success(
            "Transfer Complete",
            f"Successfully sent **{amount:,}** bytes to {receiver.mention}",
            fields=[
                ("New Balance", f"{result['giver_new_balance']:,} bytes", True),
                ("Total Sent", f"{result['giver_total_sent']:,} bytes", True)
            ]
        )
        
        if reason:
            embed.add_field("Reason", reason, inline=False)
        
        await ctx.respond(embed=embed)
        
    except InsufficientBytesError as e:
        await bytes_plugin.send_error(ctx, e.user_message)
    except ValueError as e:
        await bytes_plugin.send_error(ctx, str(e))
    except Exception as e:
        logger.error("Transfer failed", error=str(e))
        await bytes_plugin.send_error(
            ctx,
            "Failed to transfer bytes. Please try again later."
        )
```

### API Transfer Implementation
Backend API for secure transfer execution:

```python
@router.post("/guilds/{guild_id}/bytes/transfer", response_model=BytesTransferResponse)
async def transfer_bytes(
    guild_id: str,
    request: BytesTransferRequest,
    api_key: CurrentAPIKey,
    db: DatabaseSession
) -> BytesTransferResponse:
    """Transfer bytes between users."""
    # Get giver balance
    giver = await bytes_crud.get(
        db,
        guild_id=guild_id,
        user_id=request.giver_id
    )
    
    if not giver or giver.balance < request.amount:
        raise HTTPException(400, "Insufficient balance")
    
    # Get or create receiver
    receiver = await bytes_crud.get_or_create_balance(
        db,
        guild_id=guild_id,
        user_id=request.receiver_id,
        starting_balance=0  # Don't give starting balance on receive
    )
    
    # Update balances
    giver.balance -= request.amount
    giver.total_sent += request.amount
    
    receiver.balance += request.amount
    receiver.total_received += request.amount
    
    # Log transaction
    transaction = BytesTransaction(
        guild_id=guild_id,
        giver_id=request.giver_id,
        giver_username=request.giver_username,
        receiver_id=request.receiver_id,
        receiver_username=request.receiver_username,
        amount=request.amount,
        reason=request.reason
    )
    db.add(transaction)
    
    await db.commit()
    
    return BytesTransferResponse(
        transaction_id=str(transaction.id),
        giver_new_balance=giver.balance,
        receiver_new_balance=receiver.balance,
        giver_total_sent=giver.total_sent,
        receiver_total_received=receiver.total_received
    )
```

## Related Files
- `bot/services/bytes_service.py` - Transfer and balance logic
- `bot/plugins/bytes.py` - Discord command implementation
- `bot/views/bytes_views.py` - Interactive confirmation views
- `web/api/routers/bytes.py` - Transfer API endpoints
- `web/api/schemas.py` - Transfer request/response models
- `bot/errors.py` - Custom error types

## Goals Achieved
- **Secure Transfers**: Comprehensive validation prevents errors
- **User-Friendly Interface**: Clear commands and confirmation flows
- **Audit Trail**: All transfers logged with full details
- **Configurable Limits**: Guild-specific transfer restrictions
- **Interactive Confirmations**: Prevents accidental large transfers

## Dependencies
- Database models for balance and transaction tracking
- API client for backend communication
- Discord interaction system for confirmations
- Error handling system for user feedback
- Cooldown system to prevent spam

## Testing Strategy
```python
@pytest.mark.asyncio
async def test_transfer_validation(bytes_service, mock_api):
    """Test transfer validation."""
    # Self transfer
    with pytest.raises(ValueError, match="Cannot transfer bytes to yourself"):
        await bytes_service.transfer(
            "guild1", "user1", "User1", "user1", "User1", 100
        )
    
    # Negative amount
    with pytest.raises(ValueError, match="Amount must be positive"):
        await bytes_service.transfer(
            "guild1", "user1", "User1", "user2", "User2", -10
        )
```

This balance and transfer management system provides users with complete control over their bytes through secure, validated transfer operations and comprehensive balance tracking.