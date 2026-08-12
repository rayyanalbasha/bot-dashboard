import os
from collections import defaultdict
from datetime import timedelta
import discord
from discord import app_commands
from discord.ext import commands

import shared_config as cfg

# Initialize intents
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True
intents.moderation = True  # Required for timeout/ban/kick

class SecurityBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

        # NOTE: punishment settings, bad words, log channel, verification,
        # protection toggles, and bypass permissions all live in config.db
        # (via shared_config.py), keyed by guild id, so they stay in sync
        # with the web dashboard and survive restarts. Nothing guild-specific
        # is cached on the bot object anymore -- everything below is looked
        # up per-guild at the moment it's needed.
        self.warnings = defaultdict(int)
        self.message_logs = defaultdict(list)

        # Security tracking dictionaries (purely runtime/rate-limit state,
        # not persisted config, so these stay as in-memory dicts)
        self.everyone_logs = defaultdict(list)       # Member ID -> list of @everyone mention timestamps (admins, 1hr window)
        self.image_logs = defaultdict(list)          # Member ID -> list of (timestamp, channel_id, img_signature)
        self.manual_warnings = defaultdict(int)      # Member ID -> warn count from /تحذير command

    async def setup_hook(self):
        cfg.init_db()
        await self.tree.sync()
        print(f"Synced slash commands for {self.user}")

bot = SecurityBot()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")

def is_immune(member: discord.Member) -> bool:
    if member.id == member.guild.owner_id:
        return True
    if member.id == bot.user.id:
        return True
    return False

# Permission check: User must be Admin and higher in the role hierarchy than the bot
def can_manage_panel(member: discord.Member, guild: discord.Guild) -> tuple[bool, str]:
    if member.id == guild.owner_id:
        return True, ""

    if not member.guild_permissions.administrator:
        return False, "❌ ليس لديك صلاحية Administrator لاستخدام هذا الأمر."

    bot_member = guild.me
    if member.top_role.position <= bot_member.top_role.position:
        return False, "❌ لا يمكنك استخدام هذه اللوحة لأن رتبتك الإدارية أقل من أو مساوية لرتبة البوت."

    return True, ""

# Helper function to send log messages
async def send_log_channel(guild: discord.Guild, text: str):
    try:
        channel_id = cfg.get_log_channel(guild.id)
        log_channel = guild.get_channel(int(channel_id)) if channel_id else None

        if not log_channel:
            return

        if log_channel and log_channel.permissions_for(guild.me).send_messages:
            await log_channel.send(text)
    except Exception as e:
        print(f"Failed to send log: {e}")

async def execute_punishment(member: discord.Member, action_type: str, reason: str):
    if is_immune(member):
        return
    if cfg.is_bypassed(member.guild.id, member.id, action_type):
        return

    setting = cfg.get_punishment(member.guild.id, action_type)
    if setting == "disabled" or not member.guild.me.guild_permissions.administrator:
        return

    try:
        if setting == "ban":
            await member.ban(reason=reason)
            await send_log_channel(member.guild, f"🚨 **[BAN]** تم حظر العضو {member.mention} (`{member.id}`). السبب: `{reason}` (نوع الحدث: `{action_type}`)")
        elif setting == "kick":
            await member.kick(reason=reason)
            await send_log_channel(member.guild, f"🚨 **[KICK]** تم طرد العضو {member.mention} (`{member.id}`). السبب: `{reason}` (نوع الحدث: `{action_type}`)")
    except Exception as e:
        print(f"Failed to punish {member}: {e}")

# ==================== Event Listeners ====================

@bot.event
async def on_guild_update(before: discord.Guild, after: discord.Guild):
    if not after.me.guild_permissions.view_audit_log:
        return

    async for entry in after.audit_logs(limit=1, action=discord.AuditLogAction.guild_update):
        if entry.user and not entry.user.bot:
            if entry.user.id != after.owner_id:
                try:
                    revert_kwargs = {}
                    if before.name != after.name:
                        revert_kwargs["name"] = before.name
                    if before.description != after.description:
                        revert_kwargs["description"] = before.description
                    if before.icon != after.icon:
                        revert_kwargs["icon"] = before.icon
                    if before.banner != after.banner:
                        revert_kwargs["banner"] = before.banner

                    if revert_kwargs:
                        await after.edit(**revert_kwargs, reason="Unauthorized server modification by admin")

                    user_member = after.get_member(entry.user.id)
                    if user_member and not is_immune(user_member):
                        await execute_punishment(user_member, "server_change", "Attempted to modify server details/name")

                    await send_log_channel(
                        after,
                        f"🛡️ **[SERVER EDIT REVERTED]** قام الإداري {entry.user.mention} بتعديل معلومات/اسم السيرفر وتم إرجاعها تلقائياً (المالك فقط هو المسموح له)."
                    )
                except Exception as e:
                    print(f"Failed to revert server update: {e}")
            break

@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild

    # New-Account-Age Protection: ban on join if the account is younger
    # than 48 hours and the protection is enabled for this guild.
    if not is_immune(member) and cfg.get_account_age_protection(guild.id) == "enabled":
        now = discord.utils.utcnow()
        account_age = now - member.created_at
        if account_age < timedelta(hours=48):
            try:
                await member.ban(reason="Account age protection: account created less than 48 hours ago")
                await send_log_channel(
                    guild,
                    f"🚨 **[ACCOUNT AGE]** تم حظر العضو {member.mention} (`{member.id}`) تلقائياً لأن حسابه تم إنشاؤه منذ أقل من 48 ساعة."
                )
            except Exception as e:
                print(f"Failed to ban new/young account {member}: {e}")
            return

    verif_data = cfg.get_verification(guild.id)

    if verif_data:
        verif_role = guild.get_role(verif_data["role_id"])
        if verif_role:
            try:
                await member.add_roles(verif_role, reason="New member verification pending")
            except Exception as e:
                print(f"Failed to give verification role: {e}")

    if member.bot:
        async for entry in member.guild.audit_logs(limit=1, action=discord.AuditLogAction.bot_add):
            adder = entry.user
            if adder and not adder.bot:
                adder_member = member.guild.get_member(adder.id)
                if adder_member and not is_immune(adder_member):
                    await execute_punishment(adder_member, "bot_add", "Added an unauthorized bot")
                    await execute_punishment(member, "bot_add", "Unauthorized bot join")
                    await send_log_channel(member.guild, f"⚠️ **[BOT ADD]** قام العضو {adder_member.mention} بإضافة بوت غير مسموح به: {member.mention}")
                break

@bot.event
async def on_member_remove(member: discord.Member):
    guild = member.guild
    if not guild.me.guild_permissions.view_audit_log:
        return

    async for entry in guild.audit_logs(limit=1):
        if entry.target.id == member.id:
            if entry.action == discord.AuditLogAction.ban:
                if entry.user and not entry.user.bot:
                    user_member = guild.get_member(entry.user.id)
                    if user_member and not is_immune(user_member):
                        await execute_punishment(user_member, "member_ban", "Banned another user")
                        await send_log_channel(guild, f"🛡️ **[MEMBER BAN]** قام العضو {user_member.mention} بحظر عضو آخر ({member.name}).")
            elif entry.action == discord.AuditLogAction.kick:
                if entry.user and not entry.user.bot:
                    user_member = guild.get_member(entry.user.id)
                    if user_member and not is_immune(user_member):
                        await execute_punishment(user_member, "member_kick", "Kicked another user")
                        await send_log_channel(guild, f"🛡️ **[MEMBER KICK]** قام العضو {user_member.mention} بطرد عضو آخر ({member.name}).")
            break

@bot.event
async def on_guild_channel_create(channel):
    await handle_channel_audit(channel.guild, "إنشاء روم")

@bot.event
async def on_guild_channel_delete(channel):
    await handle_channel_audit(channel.guild, "حذف روم")

@bot.event
async def on_guild_channel_update(before, after):
    await handle_channel_audit(after.guild, "تعديل روم")

async def handle_channel_audit(guild, action_name):
    if cfg.get_punishment(guild.id, "channel_change") == "disabled":
        return
    async for entry in guild.audit_logs(limit=1):
        if entry.action in [
            discord.AuditLogAction.channel_create,
            discord.AuditLogAction.channel_delete,
            discord.AuditLogAction.channel_update,
        ]:
            if entry.user and not entry.user.bot:
                user_member = guild.get_member(entry.user.id)
                if user_member and not is_immune(user_member):
                    await execute_punishment(user_member, "channel_change", f"Created/Deleted/Modified a channel ({action_name})")
                    await send_log_channel(guild, f"📁 **[CHANNEL AUDIT]** قام العضو {user_member.mention} بـ ({action_name}).")
            break

@bot.event
async def on_guild_role_create(role):
    await handle_role_audit(role.guild, role)

@bot.event
async def on_guild_role_delete(role):
    await handle_role_audit(role.guild, role)

@bot.event
async def on_guild_role_update(before, after):
    if before.name != after.name:
        await handle_role_audit(after.guild, after)

async def handle_role_audit(guild, role):
    if cfg.get_punishment(guild.id, "role_change") == "disabled":
        return
    async for entry in guild.audit_logs(limit=1):
        if entry.action in [
            discord.AuditLogAction.role_create,
            discord.AuditLogAction.role_delete,
            discord.AuditLogAction.role_update,
        ]:
            user = entry.user
            if user and not user.bot:
                member = guild.get_member(user.id)
                if member and not is_immune(member) and not cfg.is_bypassed(guild.id, member.id, "role_change"):
                    try:
                        if role in member.roles:
                            await member.remove_roles(role)
                    except:
                        pass
                    bot.warnings[member.id] += 1
                    if bot.warnings[member.id] == 1:
                        try:
                            for channel in guild.text_channels:
                                if channel.permissions_for(guild.me).send_messages:
                                    await channel.send(
                                        f"التحذير الاول والاخير ل {member.mention} , السبب:\nاعطاء /حذف/ تعديل رول"
                                    )
                                    break
                        except:
                            pass
                        await send_log_channel(guild, f"⚠️ **[ROLE WARNING]** تحذير أول للعضو {member.mention} بسبب العبث بالرولات.")
                    else:
                        bot.warnings[member.id] = 0
                        await member.ban(reason="Repeated role modification/creation")
                        await send_log_channel(guild, f"🚨 **[ROLE BAN]** تم حظر العضو {member.mention} لتكرار العبث بالرولات.")
            break

@bot.event
async def on_guild_emojis_update(guild, before, after):
    if cfg.get_punishment(guild.id, "emoji_change") == "disabled":
        return
    async for entry in guild.audit_logs(limit=1):
        if entry.action in [
            discord.AuditLogAction.emoji_create,
            discord.AuditLogAction.emoji_delete,
        ]:
            if entry.user and not entry.user.bot:
                user_member = guild.get_member(entry.user.id)
                if user_member and not is_immune(user_member):
                    await execute_punishment(user_member, "emoji_change", "Added or removed an emoji")
                    await send_log_channel(guild, f"😀 **[EMOJI AUDIT]** قام العضو {user_member.mention} بتعديل الإيموجيات وتمت معاقبته.")
            break

@bot.event
async def on_member_unban(guild, user):
    if cfg.get_punishment(guild.id, "unban") == "disabled":
        return
    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.unban):
        if entry.target.id == user.id:
            if entry.user and not entry.user.bot:
                member = guild.get_member(entry.user.id)
                if member and not is_immune(member):
                    await execute_punishment(member, "unban", "Unbanned a user")
                    await send_log_channel(guild, f"🔓 **[UNBAN AUDIT]** قام العضو {member.mention} بفك الحظر عن عضو آخر ({user.name}).")
            break

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    member = message.guild.get_member(message.author.id)
    if not member:
        await bot.process_commands(message)
        return

    now = discord.utils.utcnow()

    # @everyone / @here Mention Protection
    # - Regular (non-admin) member: any mention -> immediate 5 minute timeout.
    # - Administrator whose top role is below the bot's top role: 3 mentions
    #   within a rolling 1 hour window -> 5 minute timeout.
    if message.mention_everyone and not is_immune(member):
        if (
            cfg.get_everyone_protection(message.guild.id) == "enabled"
            and not cfg.is_bypassed(message.guild.id, member.id, "everyone_mention")
        ):
            bot_top_position = message.guild.me.top_role.position
            bot_can_act_on_member = member.top_role.position < bot_top_position

            if bot_can_act_on_member:
                if not member.guild_permissions.administrator:
                    try:
                        await member.timeout(now + timedelta(minutes=5), reason="Mentioned @everyone/@here")
                        warning_msg = await message.channel.send(f"⏳ {member.mention}, تم إعطاؤك **تايم أوت لمدة 5 دقائق** بسبب منشن @everyone/@here.")
                        await warning_msg.delete(delay=5)
                        await send_log_channel(message.guild, f"⚠️ **[EVERYONE MENTION]** تم إعطاء تايم أوت 5 دقائق للعضو {member.mention} بسبب منشن @everyone/@here.")
                    except Exception as e:
                        print(f"Error executing @everyone timeout: {e}")
                    return
                else:
                    stamps = bot.everyone_logs[member.id]
                    stamps = [t for t in stamps if now - t < timedelta(hours=1)]
                    stamps.append(now)
                    bot.everyone_logs[member.id] = stamps

                    if len(stamps) >= 3:
                        try:
                            await member.timeout(now + timedelta(minutes=5), reason="Admin mentioned @everyone/@here 3 times within an hour")
                            warning_msg = await message.channel.send(f"⏳ {member.mention}, تم إعطاؤك **تايم أوت لمدة 5 دقائق** بسبب تكرار منشن @everyone/@here (3 مرات خلال ساعة).")
                            await warning_msg.delete(delay=5)
                            await send_log_channel(message.guild, f"⚠️ **[ADMIN EVERYONE MENTION]** تم إعطاء تايم أوت 5 دقائق للإداري {member.mention} لتكراره منشن @everyone/@here 3 مرات خلال ساعة.")
                        except Exception as e:
                            print(f"Error executing admin @everyone timeout: {e}")
                        bot.everyone_logs[member.id] = []
                    return

    # Cross-Channel Image Spam Protection
    if message.attachments and not is_immune(member):
        for attachment in message.attachments:
            is_image = attachment.content_type and attachment.content_type.startswith("image")
            if not is_image and any(attachment.filename.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp"]):
                is_image = True

            if is_image:
                img_sig = f"{attachment.filename}_{attachment.size}"
                img_stamps = bot.image_logs[member.id]
                img_stamps = [item for item in img_stamps if now - item[0] < timedelta(minutes=1)]
                img_stamps.append((now, message.channel.id, img_sig))
                bot.image_logs[member.id] = img_stamps

                unique_channels = set(ch_id for _, ch_id, sig in img_stamps if sig == img_sig)

                if len(unique_channels) >= 4:
                    try:
                        await member.ban(reason="Cross-channel image spam (same image sent in 4 channels in 1 min)")
                        await send_log_channel(message.guild, f"🚨 **[IMAGE SPAM BAN]** تم حظر العضو {member.mention} بسبب تكرار إرسال نفس الصورة في 4 رومات مختلفة خلال دقيقة.")
                        bot.image_logs[member.id] = []
                        return
                    except Exception as e:
                        print(f"Failed to ban image spammer: {e}")

    # Message Spam Protection
    if not is_immune(member):
        user_timestamps = bot.message_logs[message.author.id]
        user_timestamps = [t for t in user_timestamps if now - t < timedelta(seconds=15)]
        user_timestamps.append(now)
        bot.message_logs[message.author.id] = user_timestamps

        if len(user_timestamps) >= 5:
            try:
                await member.timeout(now + timedelta(minutes=5), reason="Spamming messages (5 messages in 15 seconds)")
                warning_msg = await message.channel.send(f"⏳ {member.mention}, تم إعطاؤك **تايم أوت لمدة 5 دقائق** بسبب سبام الرسائل.")
                await warning_msg.delete(delay=5)
                await send_log_channel(message.guild, f"⏳ **[SPAM]** تم إعطاء تايم أوت 5 دقائق للعضو {member.mention} بسبب سبام الرسائل في روم {message.channel.mention}.")
            except Exception as e:
                print(f"CRITICAL TIMEOUT ERROR for {member.name}: {e}")

            bot.message_logs[message.author.id] = []
            return

    # Bad Words Substring Filter
    if not is_immune(member):
        content_lower = message.content.lower()
        bad_words = cfg.get_bad_words(message.guild.id)
        contains_bad_word = any(bad_word.strip().lower() in content_lower for bad_word in bad_words if bad_word.strip())

        if contains_bad_word:
            try:
                await message.delete()
                await member.timeout(now + timedelta(hours=1), reason="استخدام ألفاظ نابية / سب")
                warning_msg = await message.channel.send(f"⚠️ {member.mention}, تم إعطاؤك **تايم أوت لمدة ساعة** لاستخدامك كلمات ممنوعة.")
                await warning_msg.delete(delay=5)
                await send_log_channel(message.guild, f"💬 **[BAD WORD]** تم إعطاء تايم أوت لمدة ساعة للعضو {member.mention} لاستخدامه كلمة ممنوعة في روم {message.channel.mention}.")
            except Exception as e:
                print(f"Failed to punish bad word sender: {e}")
            return

    await bot.process_commands(message)


# ==================== Moderation Commands ====================

@bot.tree.command(name="خاص", description="جعل الروم خاصاً للإداريين فقط (إخفاء الروم عن الأعضاء)")
async def private_command(interaction: discord.Interaction):
    allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
    if not allowed:
        await interaction.response.send_message(error_msg, ephemeral=True)
        return

    channel = interaction.channel
    everyone_role = interaction.guild.default_role

    try:
        overwrite = channel.overwrites_for(everyone_role)
        overwrite.view_channel = False
        await channel.set_permissions(everyone_role, overwrite=overwrite, reason=f"Made private by {interaction.user}")

        await interaction.response.send_message("🕵️ **تم تحويل هذه الروم إلى خاصة!** تظهر الآن للإداريين فقط.")
        await send_log_channel(
            interaction.guild,
            f"🕵️ **[PRIVATE]** قام الإداري {interaction.user.mention} بتحويل الروم {channel.mention} إلى خاصة."
        )
    except Exception as e:
        await interaction.response.send_message(f"❌ حدث خطأ أثناء تحويل الروم إلى خاصة: {e}", ephemeral=True)


@bot.tree.command(name="عام", description="جعل الروم عاماً ومرئياً لجميع الأعضاء مرة أخرى")
async def public_command(interaction: discord.Interaction):
    allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
    if not allowed:
        await interaction.response.send_message(error_msg, ephemeral=True)
        return

    channel = interaction.channel
    everyone_role = interaction.guild.default_role

    try:
        overwrite = channel.overwrites_for(everyone_role)
        overwrite.view_channel = None
        await channel.set_permissions(everyone_role, overwrite=overwrite, reason=f"Made public by {interaction.user}")

        await interaction.response.send_message("🌐 **تم تحويل هذه الروم إلى عامة!** أصبحت مرئية للجميع الآن.")
        await send_log_channel(
            interaction.guild,
            f"🌐 **[PUBLIC]** قام الإداري {interaction.user.mention} بتحويل الروم {channel.mention} إلى عامة."
        )
    except Exception as e:
        await interaction.response.send_message(f"❌ حدث خطأ أثناء تحويل الروم إلى عامة: {e}", ephemeral=True)


@bot.tree.command(name="قفل", description="قفل الروم لمنع غير الإداريين من إرسال الرسائل")
async def lock_command(interaction: discord.Interaction):
    allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
    if not allowed:
        await interaction.response.send_message(error_msg, ephemeral=True)
        return

    channel = interaction.channel
    everyone_role = interaction.guild.default_role

    try:
        overwrite = channel.overwrites_for(everyone_role)
        overwrite.send_messages = False
        await channel.set_permissions(everyone_role, overwrite=overwrite, reason=f"Locked by {interaction.user}")

        await interaction.response.send_message("🔒 **تم قفل هذه الروم بنجاح!** يمكن للإداريين فقط الكتابة الآن.")
        await send_log_channel(
            interaction.guild,
            f"🔒 **[LOCK]** قام الإداري {interaction.user.mention} بقفل الروم {channel.mention}."
        )
    except Exception as e:
        await interaction.response.send_message(f"❌ حدث خطأ أثناء قفل الروم: {e}", ephemeral=True)


@bot.tree.command(name="فتح", description="فتح الروم وسمح للجميع بالكتابة مرة أخرى")
async def unlock_command(interaction: discord.Interaction):
    allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
    if not allowed:
        await interaction.response.send_message(error_msg, ephemeral=True)
        return

    channel = interaction.channel
    everyone_role = interaction.guild.default_role

    try:
        overwrite = channel.overwrites_for(everyone_role)
        overwrite.send_messages = None
        await channel.set_permissions(everyone_role, overwrite=overwrite, reason=f"Unlocked by {interaction.user}")

        await interaction.response.send_message("🔓 **تم فتح هذه الروم بنجاح!** يمكن للجميع الكتابة الآن.")
        await send_log_channel(
            interaction.guild,
            f"🔓 **[UNLOCK]** قام الإداري {interaction.user.mention} بفتح الروم {channel.mention}."
        )
    except Exception as e:
        await interaction.response.send_message(f"❌ حدث خطأ أثناء فتح الروم: {e}", ephemeral=True)


@bot.tree.command(name="مسح", description="مسح عدد محدد من الرسائل في هذا الروم")
@app_commands.describe(amount="عدد الرسائل التي تريد مسحها من هذا الروم (1 - 100)")
async def purge_command(interaction: discord.Interaction, amount: int):
    allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
    if not allowed:
        await interaction.response.send_message(error_msg, ephemeral=True)
        return

    if amount < 1 or amount > 100:
        await interaction.response.send_message("❌ يرجى اختيار عدد رسائل بين 1 و 100.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"✅ تم مسح **{len(deleted)}** رسالة بنجاح!", ephemeral=True)
        await send_log_channel(
            interaction.guild,
            f"🗑️ **[PURGE]** قام الإداري {interaction.user.mention} بمسح **{len(deleted)}** رسالة من روم {interaction.channel.mention}."
        )
    except Exception as e:
        await interaction.followup.send(f"❌ حدث خطأ أثناء مسح الرسائل: {e}", ephemeral=True)


@bot.tree.command(name="تحذير", description="إعطاء تحذير لعضو (في التحذير الثاني يحصل على تايم أوت 5 دقائق)")
@app_commands.describe(member="اختر العضو المراد تحذيره", reason="سبب التحذير")
async def warn_command(interaction: discord.Interaction, member: discord.Member, reason: str):
    allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
    if not allowed:
        await interaction.response.send_message(error_msg, ephemeral=True)
        return

    if is_immune(member):
        await interaction.response.send_message("❌ لا يمكنك إعطاء تحذير لهذا العضو.", ephemeral=True)
        return

    bot.manual_warnings[member.id] += 1
    current_warns = bot.manual_warnings[member.id]

    if current_warns == 1:
        await interaction.response.send_message(
            f"⚠️ تم إعطاء التحذير الأول للعضو {member.mention}.\nالسبب: `{reason}`"
        )
        await send_log_channel(interaction.guild, f"⚠️ **[WARN]** قام الإداري {interaction.user.mention} بتحذير العضو {member.mention} (التحذير 1). السبب: `{reason}`")
    else:
        bot.manual_warnings[member.id] = 0
        try:
            now = discord.utils.utcnow()
            await member.timeout(now + timedelta(minutes=5), reason=f"وصل للتحذير الثاني: {reason}")
            await interaction.response.send_message(
                f"🚨 وصل العضو {member.mention} للتحذير الثاني، وتم إعطاؤه **تايم أوت لمدة 5 دقائق** تلقائياً.\nالسبب: `{reason}`"
            )
            await send_log_channel(interaction.guild, f"🚨 **[WARN TIMEOUT]** تم تطبيق تايم أوت 5 دقائق على العضو {member.mention} لتراكم التحذيرات بواسطة {interaction.user.mention}.")
        except Exception as e:
            await interaction.response.send_message(f"❌ حدث خطأ أثناء تطبيق التايم أوت: {e}", ephemeral=True)


@bot.tree.command(name="تايم", description="إعطاء تايم أوت لعضو محدد مع إمكانية تحديد المدة بالدقائق والسبب")
@app_commands.describe(member="اختر العضو المراد معاقبته", duration="المدة بالدقائق", reason="سبب العقوبة")
async def timeout_command(interaction: discord.Interaction, member: discord.Member, duration: int, reason: str = "بدون سبب"):
    allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
    if not allowed:
        await interaction.response.send_message(error_msg, ephemeral=True)
        return

    if is_immune(member) or member.top_role.position >= interaction.guild.me.top_role.position:
        await interaction.response.send_message("❌ لا يمكنك تطبيق تايم أوت على هذا العضو.", ephemeral=True)
        return

    try:
        now = discord.utils.utcnow()
        await member.timeout(now + timedelta(minutes=duration), reason=reason)
        await interaction.response.send_message(
            f"⏳ تم إعطاء **تايم أوت لمدة {duration} دقيقة** للعضو {member.mention}.\nالسبب: `{reason}`"
        )
        await send_log_channel(interaction.guild, f"⏳ **[TIMEOUT]** قام الإداري {interaction.user.mention} بإعطاء تايم أوت ({duration} دقيقة) للعضو {member.mention}. السبب: `{reason}`")
    except Exception as e:
        await interaction.response.send_message(f"❌ حدث خطأ أثناء تطبيق التايم أوت: {e}", ephemeral=True)


@bot.tree.command(name="طرد", description="طرد عضو من السيرفر")
@app_commands.describe(member="اختر العضو المراد طرده", reason="سبب الطرد")
async def kick_command(interaction: discord.Interaction, member: discord.Member, reason: str = "بدون سبب"):
    allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
    if not allowed:
        await interaction.response.send_message(error_msg, ephemeral=True)
        return

    if is_immune(member) or member.top_role.position >= interaction.guild.me.top_role.position:
        await interaction.response.send_message("❌ لا يمكنك طرد هذا العضو.", ephemeral=True)
        return

    try:
        await member.kick(reason=reason)
        await interaction.response.send_message(f"👢 تم طرد العضو **{member.name}** (`{member.id}`) بنجاح.\nالسبب: `{reason}`")
        await send_log_channel(interaction.guild, f"👢 **[KICK]** قام الإداري {interaction.user.mention} بطرد العضو {member.name} (`{member.id}`). السبب: `{reason}`")
    except Exception as e:
        await interaction.response.send_message(f"❌ حدث خطأ أثناء طرد العضو: {e}", ephemeral=True)


@bot.tree.command(name="حظر", description="حظر عضو من السيرفر")
@app_commands.describe(member="اختر العضو المراد حظره", reason="سبب الحظر")
async def ban_command(interaction: discord.Interaction, member: discord.Member, reason: str = "بدون سبب"):
    allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
    if not allowed:
        await interaction.response.send_message(error_msg, ephemeral=True)
        return

    if is_immune(member) or member.top_role.position >= interaction.guild.me.top_role.position:
        await interaction.response.send_message("❌ لا يمكنك حظر هذا العضو.", ephemeral=True)
        return

    try:
        await member.ban(reason=reason)
        await interaction.response.send_message(f"🔨 تم حظر العضو **{member.name}** (`{member.id}`) بنجاح.\nالسبب: `{reason}`")
        await send_log_channel(interaction.guild, f"🔨 **[BAN]** قام الإداري {interaction.user.mention} بحظر العضو {member.name} (`{member.id}`). السبب: `{reason}`")
    except Exception as e:
        await interaction.response.send_message(f"❌ حدث خطأ أثناء حظر العضو: {e}", ephemeral=True)


# ==================== Verification System ====================

class VerifyButtonView(discord.ui.View):
    def __init__(self, unverified_role_id: int):
        super().__init__(timeout=None)
        self.unverified_role_id = unverified_role_id

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.primary, custom_id="verify_button_click")
    async def verify_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        member = interaction.user

        role = guild.get_role(self.unverified_role_id)
        if role:
            try:
                await member.remove_roles(role, reason="User verified")
                await interaction.response.send_message("✅ تم التحقق بنجاح! ظهرت لك الآن باقي رومات السيرفر.", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"❌ حدث خطأ أثناء إزالة رتبة التحقق: {e}", ephemeral=True)
        else:
            await interaction.response.send_message("❌ رتبة التحقق غير موجودة أو تم حذفها.", ephemeral=True)


@bot.tree.command(name="روم_تحقق", description="إنشاء روم وإعداد نظام التحقق الجديد للأعضاء الجدد")
@app_commands.describe(channel_name="اسم روم التحقق (الافتراضي: verify)")
async def setup_verification(interaction: discord.Interaction, channel_name: str = "verify"):
    allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
    if not allowed:
        await interaction.response.send_message(error_msg, ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    try:
        unverified_role = await guild.create_role(name="Unverified", reason="Verification System Role")

        for channel in guild.channels:
            try:
                await channel.set_permissions(unverified_role, view_channel=False)
            except:
                pass

        verif_channel_overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            unverified_role: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True)
        }

        verif_channel = await guild.create_text_channel(name=channel_name, overwrites=verif_channel_overwrites)

        cfg.set_verification(guild.id, unverified_role.id, verif_channel.id)

        embed = discord.Embed(
            title="🔒 Verification / نظام التحقق",
            description="Welcome to the server! Please click the **Verify** button below to access all channels.\n\nمرحباً بك في السيرفر! للوصول إلى باقي الرومات، يرجى الضغط على زر **Verify** أدناه.",
            color=discord.Color.blue()
        )
        embed.set_image(url="https://kommodo.ai/i/K4CO34E9sxXUAyowKqc5")

        view = VerifyButtonView(unverified_role.id)
        await verif_channel.send(embed=embed, view=view)

        await interaction.followup.send(f"✅ تم إعداد نظام التحقق بنجاح وإنشاء الروم {verif_channel.mention}!")

    except Exception as e:
        await interaction.followup.send(f"❌ حدث خطأ أثناء إعداد نظام التحقق: {e}")


@bot.tree.command(name="تعطيل_التحقق", description="حذف نظام التحقق وإعادة ظهور الرومات لجميع الأعضاء")
async def disable_verification(interaction: discord.Interaction):
    allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
    if not allowed:
        await interaction.response.send_message(error_msg, ephemeral=True)
        return

    guild = interaction.guild
    verif_data = cfg.get_verification(guild.id)

    if not verif_data:
        await interaction.response.send_message("❌ لا يوجد نظام تحقق مفعل حالياً في هذا السيرفر.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        channel = guild.get_channel(verif_data["channel_id"])
        if channel:
            await channel.delete(reason="Verification system removed")

        role = guild.get_role(verif_data["role_id"])
        if role:
            await role.delete(reason="Verification system removed")

        cfg.clear_verification(guild.id)

        await interaction.followup.send("✅ تم تعطيل نظام التحقق بنجاح وإعادة الصلاحيات للوضع الطبيعي.")
        await send_log_channel(guild, f"🛡️ **[VERIFICATION REMOVED]** قام الإداري {interaction.user.mention} بحذف نظام التحقق بالكامل.")

    except Exception as e:
        await interaction.followup.send(f"❌ حدث خطأ أثناء إزالة النظام: {e}")


# ==================== Log Channel Command ====================

@bot.tree.command(name="لوقات_عقابات", description="اختيار الروم المخصص لتلقي جميع لوجات وعقوبات البوت")
@app_commands.describe(room="اختر الروم النصي المخصص للوجات")
async def log_channel_command(interaction: discord.Interaction, room: discord.TextChannel):
    allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
    if not allowed:
        await interaction.response.send_message(error_msg, ephemeral=True)
        return

    cfg.set_log_channel(interaction.guild.id, room.id)

    await interaction.response.send_message(
        f"✅ تم تحديد روم اللوجات بنجاح إلى: {room.mention}",
        ephemeral=True
    )
    await send_log_channel(
        interaction.guild,
        f"📌 **[LOG CHANNEL]** تم تعيين هذه الروم بواسطة الإداري {interaction.user.mention} لتلقي جميع تقارير وعقوبات البوت."
    )


# ==================== Bad Words Filter Dashboard ====================

class MultiWordAddModal(discord.ui.Modal):
    def __init__(self, guild_id: int):
        super().__init__(title="إضافة كلمات جديدة للفلتر")
        self.guild_id = guild_id

        self.words_input = discord.ui.TextInput(
            label="اكتب الكلمات (افصل بينها بمسافة أو سطر جديد)",
            style=discord.TextStyle.paragraph,
            placeholder="مثال:\nكلمة1 كلمة2\nكلمة3",
            required=True,
            max_length=4000,
        )
        self.add_item(self.words_input)

    async def on_submit(self, interaction: discord.Interaction):
        allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
        if not allowed:
            await interaction.response.send_message(error_msg, ephemeral=True)
            return

        words_to_add = self.words_input.value.split()
        added_count, already_exists = cfg.add_bad_words(self.guild_id, words_to_add)

        msg = f"✅ تمت إضافة **{added_count}** كلمة بنجاح للفلتر!"
        if already_exists > 0:
            msg += f"\n(تم تخطي {already_exists} كلمات موجودة مسبقاً)."

        await interaction.response.send_message(msg, ephemeral=True)
        await send_log_channel(interaction.guild, f"⚙️ **[FILTER UPDATE]** قام الإداري {interaction.user.mention} بإضافة {added_count} كلمات جديدة للفلتر.")


class RemoveWordSelect(discord.ui.Select):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        bad_words = cfg.get_bad_words(guild_id)

        if not bad_words:
            options = [discord.SelectOption(label="الفارغ حالياً", value="empty")]
        else:
            options = [discord.SelectOption(label=w, value=w) for w in bad_words[:25]]

        super().__init__(
            placeholder="اختر كلمة لحذفها من الفلتر...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
        if not allowed:
            await interaction.response.send_message(error_msg, ephemeral=True)
            return

        if self.values[0] == "empty":
            await interaction.response.send_message("❌ الفلتر فارغ أساساً.", ephemeral=True)
            return

        word_to_remove = self.values[0]
        if cfg.remove_bad_word(self.guild_id, word_to_remove):
            await interaction.response.send_message(
                f"🗑️ تمت إزالة الكلمة `{word_to_remove}` من الفلتر بنجاح!",
                ephemeral=True,
            )
            await send_log_channel(interaction.guild, f"⚙️ **[FILTER UPDATE]** قام الإداري {interaction.user.mention} بحذف الكلمة `{word_to_remove}` من الفلتر.")
        else:
            await interaction.response.send_message("❌ الكلمة غير موجودة.", ephemeral=True)


class FilterManagementView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.add_item(RemoveWordSelect(guild_id))

    @discord.ui.button(
        label="➕ إضافة كلمات جديدة",
        style=discord.ButtonStyle.success,
        custom_id="add_words_btn",
        row=1
    )
    async def add_words(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
        if not allowed:
            await interaction.response.send_message(error_msg, ephemeral=True)
            return
        await interaction.response.send_modal(MultiWordAddModal(self.guild_id))

    @discord.ui.button(
        label="🔄 تحديث القوائم",
        style=discord.ButtonStyle.secondary,
        custom_id="refresh_filter_panel",
        row=1
    )
    async def refresh_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
        if not allowed:
            await interaction.response.send_message(error_msg, ephemeral=True)
            return

        bad_words = cfg.get_bad_words(self.guild_id)
        words_str = ", ".join([f"`{w}`" for w in bad_words]) if bad_words else "فارغ حالياً"

        embed = discord.Embed(
            title="⚙️ لوحة إدارة فلتر المسبات المتقدمة",
            description=(
                f"**الكلمات الممنوعة حالياً:**\n{words_str}\n\n"
                "استخدم زر **➕ إضافة كلمات جديدة** لإضافة كلمات متعددة بمسافات بينها، "
                "والقائمة المنسدلة لحذف أي كلمة فوراً."
            ),
            color=discord.Color.blue(),
        )
        await interaction.response.edit_message(embed=embed, view=FilterManagementView(self.guild_id))


@bot.tree.command(name="فلتر_لوحة", description="فتح لوحة التحكم الكاملة لإضافة وحذف الكلمات")
async def filter_dashboard(interaction: discord.Interaction):
    allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
    if not allowed:
        await interaction.response.send_message(error_msg, ephemeral=True)
        return

    bad_words = cfg.get_bad_words(interaction.guild.id)
    words_str = ", ".join([f"`{w}`" for w in bad_words]) if bad_words else "فارغ حالياً"

    embed = discord.Embed(
        title="⚙️ لوحة إدارة فلتر المسبات المتقدمة",
        description=(
            f"**الكلمات الممنوعة حالياً:**\n{words_str}\n\n"
            "استخدم زر **➕ إضافة كلمات جديدة** لإضافة كلمات متعددة بمسافات بينها، "
            "والقائمة المنسدلة لحذف أي كلمة فوراً."
        ),
        color=discord.Color.blue(),
    )

    view = FilterManagementView(interaction.guild.id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ==================== Punishments Dashboard ====================

class PunishmentControlView(discord.ui.View):
    def __init__(self, guild_id: int, action_key: str):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.action_key = action_key
        self.update_buttons()

    def update_buttons(self):
        current_status = cfg.get_punishment(self.guild_id, self.action_key)
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id == "toggle_disable_btn":
                if current_status == "disabled":
                    child.label = "Enable"
                    child.style = discord.ButtonStyle.success
                else:
                    child.label = "Disable"
                    child.style = discord.ButtonStyle.secondary

    @discord.ui.button(label="Ban", style=discord.ButtonStyle.danger)
    async def set_ban(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg.set_punishment(self.guild_id, self.action_key, "ban")
        self.update_buttons()
        await interaction.response.edit_message(
            content=f"تم تغيير عقوبة `{self.action_key}` إلى **Ban**.", view=self
        )
        await send_log_channel(interaction.guild, f"🛡️ **[CONFIG UPDATE]** قام الإداري {interaction.user.mention} بتغيير عقوبة `{self.action_key}` إلى **Ban**.")

    @discord.ui.button(label="Kick", style=discord.ButtonStyle.primary)
    async def set_kick(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg.set_punishment(self.guild_id, self.action_key, "kick")
        self.update_buttons()
        await interaction.response.edit_message(
            content=f"تم تغيير عقوبة `{self.action_key}` إلى **Kick**.", view=self
        )
        await send_log_channel(interaction.guild, f"🛡️ **[CONFIG UPDATE]** قام الإداري {interaction.user.mention} بتغيير عقوبة `{self.action_key}` إلى **Kick**.")

    @discord.ui.button(
        label="Disable",
        style=discord.ButtonStyle.secondary,
        custom_id="toggle_disable_btn",
    )
    async def toggle_disable(self, interaction: discord.Interaction, button: discord.ui.Button):
        current_status = cfg.get_punishment(self.guild_id, self.action_key)
        if current_status == "disabled":
            cfg.set_punishment(self.guild_id, self.action_key, "ban")
            msg = f"تم تفعيل عقوبة `{self.action_key}` مرة أخرى (أصبحت Ban)."
        else:
            cfg.set_punishment(self.guild_id, self.action_key, "disabled")
            msg = f"تم تعطيل عقوبة `{self.action_key}`."

        self.update_buttons()
        await interaction.response.edit_message(content=msg, view=self)
        await send_log_channel(interaction.guild, f"🛡️ **[CONFIG UPDATE]** قام الإداري {interaction.user.mention} بتعديل حالة عقوبة `{self.action_key}`.")


class MainPunishmentsView(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Bot Add", description="إضافة بوت للسيرفر", value="bot_add"),
            discord.SelectOption(label="Member Ban", description="حظر عضو لعضو آخر", value="member_ban"),
            discord.SelectOption(label="Member Kick", description="طرد عضو لعضو آخر", value="member_kick"),
            discord.SelectOption(label="Channel Changes", description="تعديل/إنشاء/حذف الرومات", value="channel_change"),
            discord.SelectOption(label="Role Changes", description="تعديل/إنشاء/حذف الرولات", value="role_change"),
            discord.SelectOption(label="Emoji Changes", description="تعديل الإيموجيات", value="emoji_change"),
            discord.SelectOption(label="Server Changes", description="تعديل اسم أو إعدادات السيرفر", value="server_change"),
            discord.SelectOption(label="Unban", description="فك الحظر عن عضو", value="unban"),
        ]
        super().__init__(
            placeholder="اختر الحدث لتعديل عقوبته...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
        if not allowed:
            await interaction.response.send_message(error_msg, ephemeral=True)
            return

        selected_action = self.values[0]
        view = PunishmentControlView(interaction.guild.id, selected_action)
        current_setting = cfg.get_punishment(interaction.guild.id, selected_action)
        await interaction.response.send_message(
            f"الحدث المحدد: `{selected_action}` | العقوبة الحالية: **{current_setting}**\nاختر الإجراء الجديد:",
            view=view,
            ephemeral=True,
        )


class PunishmentsDashboard(discord.ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(MainPunishmentsView())


@bot.tree.command(name="العقوبات", description="لوحة تحكم إعدادات وعقوبات السيرفر")
async def punishments_command(interaction: discord.Interaction):
    allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
    if not allowed:
        await interaction.response.send_message(error_msg, ephemeral=True)
        return

    embed = discord.Embed(
        title="🛡️ لوحة تحكم عقوبات الحماية",
        description="اختر من القائمة أدناه الحدث الأمني الذي تريد تعديل عقوبته أو تعطيله.",
        color=discord.Color.blue(),
    )
    for action, punishment in cfg.get_all_punishments(interaction.guild.id).items():
        embed.add_field(
            name=action.replace("_", " ").title(),
            value=f"العقوبة: `{punishment}`",
            inline=True,
        )

    view = PunishmentsDashboard()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ==================== Whitelist / Bypass Permissions (القائمة البيضاء) ====================
# Grants specific members an exemption ("تصريح تجاوز الحماية") from one or
# more of the bot's punishments/protections. Only usable by admins whose
# top role sits above the bot's top role (same check as can_manage_panel).

BYPASS_ACTION_LABELS = {
    "bot_add": "إضافة بوت",
    "member_ban": "حظر عضو",
    "member_kick": "طرد عضو",
    "channel_change": "تعديل الرومات",
    "role_change": "تعديل الرولات",
    "emoji_change": "تعديل الإيموجيات",
    "unban": "فك الحظر",
    "server_change": "تعديل السيرفر",
    "everyone_mention": "منشن @everyone/@here",
}


def _bot_thumbnail_embed(title: str, description: str) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=discord.Color.blue())
    if bot.user and bot.user.display_avatar:
        embed.set_thumbnail(url=bot.user.display_avatar.url)
    return embed


class WhitelistMainView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    @discord.ui.button(label="اضافه", style=discord.ButtonStyle.success, custom_id="whitelist_add_btn")
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
        if not allowed:
            await interaction.response.send_message(error_msg, ephemeral=True)
            return

        embed = _bot_thumbnail_embed("🛡️ اختيار اللاعب", "انقر هنا لاختيار اللاعب")
        await interaction.response.send_message(embed=embed, view=SelectPlayerView(self.guild_id), ephemeral=True)

    @discord.ui.button(label="ازاله تصريح", style=discord.ButtonStyle.danger, custom_id="whitelist_remove_btn")
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
        if not allowed:
            await interaction.response.send_message(error_msg, ephemeral=True)
            return
        await send_bypass_users_list(interaction, self.guild_id)


class SelectPlayerDropdown(discord.ui.UserSelect):
    """The 'اختيار اللاعب' control -- a user picker instead of a plain
    button, since Discord has no way to pick a member from a button click
    alone."""

    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        super().__init__(placeholder="اختيار اللاعب", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
        if not allowed:
            await interaction.response.send_message(error_msg, ephemeral=True)
            return

        selected_user = self.values[0]
        embed = _bot_thumbnail_embed("🛡️ إصدار تصريح", "انقر هنا لاصدار تصريح تجاوز حمايه")
        embed.add_field(name="اللاعب المحدد", value=f"{selected_user.mention} (`{selected_user.id}`)", inline=False)
        await interaction.response.edit_message(embed=embed, view=IssuePermissionView(self.guild_id, selected_user.id))


class SelectPlayerView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.add_item(SelectPlayerDropdown(guild_id))


class PunishmentBypassSelect(discord.ui.Select):
    def __init__(self, guild_id: int, target_user_id: int):
        self.guild_id = guild_id
        self.target_user_id = target_user_id
        options = [
            discord.SelectOption(label=label, value=key)
            for key, label in BYPASS_ACTION_LABELS.items()
        ]
        super().__init__(placeholder="اختر العقوبة التي يمكنه تجاوزها...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
        if not allowed:
            await interaction.response.send_message(error_msg, ephemeral=True)
            return

        action = self.values[0]
        cfg.add_bypass_permission(self.guild_id, self.target_user_id, action)

        member_obj = interaction.guild.get_member(self.target_user_id)
        mention = member_obj.mention if member_obj else f"<@{self.target_user_id}>"
        label = BYPASS_ACTION_LABELS.get(action, action)

        await interaction.response.edit_message(
            content=f"✅ تم منح {mention} تصريح تجاوز **{label}** بنجاح.",
            embed=None,
            view=None,
        )
        await send_log_channel(
            interaction.guild,
            f"🛡️ **[WHITELIST]** قام الإداري {interaction.user.mention} بمنح {mention} تصريح تجاوز `{action}`."
        )


class IssuePermissionView(discord.ui.View):
    def __init__(self, guild_id: int, target_user_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.target_user_id = target_user_id

    @discord.ui.button(label="اصدار تصريح", style=discord.ButtonStyle.success, custom_id="issue_permission_btn")
    async def issue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
        if not allowed:
            await interaction.response.send_message(error_msg, ephemeral=True)
            return

        select_view = discord.ui.View(timeout=300)
        select_view.add_item(PunishmentBypassSelect(self.guild_id, self.target_user_id))
        await interaction.response.send_message(
            "اختر العقوبة التي يمكن لهذا العضو تجاوزها:", view=select_view, ephemeral=True
        )


class RemoveBypassButton(discord.ui.Button):
    def __init__(self, guild_id: int, user_id: str, display_name: str):
        super().__init__(label=f"إزالة - {display_name}"[:80], style=discord.ButtonStyle.danger)
        self.guild_id = guild_id
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
        if not allowed:
            await interaction.response.send_message(error_msg, ephemeral=True)
            return

        cfg.remove_bypass_user(self.guild_id, self.user_id)
        await interaction.response.edit_message(
            content=f"✅ تمت إزالة التصريح عن <@{self.user_id}> بنجاح.", embed=None, view=None
        )
        await send_log_channel(
            interaction.guild,
            f"🛡️ **[WHITELIST]** قام الإداري {interaction.user.mention} بإزالة تصريح <@{self.user_id}>."
        )


async def send_bypass_users_list(interaction: discord.Interaction, guild_id: int):
    bypass_data = cfg.get_bypass_permissions(guild_id)
    embed = _bot_thumbnail_embed("🛡️ الأعضاء الذين لديهم تصريح تجاوز الحماية", "")

    if not bypass_data:
        embed.description = "لا يوجد أي أعضاء لديهم تصريح حالياً."
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Discord views cap at 25 components -- keep this to the first 20 users
    # so there's headroom, and note if the list was truncated.
    entries = list(bypass_data.items())[:20]
    view = discord.ui.View(timeout=300)
    lines = []
    for user_id, actions in entries:
        member_obj = interaction.guild.get_member(int(user_id))
        display_name = member_obj.display_name if member_obj else user_id
        mention = member_obj.mention if member_obj else f"<@{user_id}>"
        labels = ", ".join(BYPASS_ACTION_LABELS.get(a, a) for a in actions) or "-"
        lines.append(f"{mention} — `{labels}`")
        view.add_item(RemoveBypassButton(guild_id, user_id, display_name))

    if len(bypass_data) > 20:
        lines.append(f"\n(...و {len(bypass_data) - 20} عضو آخر لم يظهر هنا)")

    embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="القائمه-البيضاء", description="إدارة تصاريح تجاوز الحماية للأعضاء")
async def whitelist_command(interaction: discord.Interaction):
    allowed, error_msg = can_manage_panel(interaction.user, interaction.guild)
    if not allowed:
        await interaction.response.send_message(error_msg, ephemeral=True)
        return

    embed = _bot_thumbnail_embed("🛡️ القائمة البيضاء - تصاريح تجاوز الحماية", "يرجى النقر اسفلا لاعطاء تصريح تجاوز الحمايه")
    await interaction.response.send_message(embed=embed, view=WhitelistMainView(interaction.guild.id), ephemeral=True)


# ==================== Entry Point ====================

if __name__ == "__main__":
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    print(f"[sec.py] BOT_TOKEN visible here: {bool(BOT_TOKEN)} (len={len(BOT_TOKEN) if BOT_TOKEN else 0})")
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set.")

    bot.run(BOT_TOKEN)