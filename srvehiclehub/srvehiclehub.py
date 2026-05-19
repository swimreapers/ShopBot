import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import discord
from redbot.core import Config, checks, commands


POINTS_CONTAINER_KEYS = ("PointsBySteamId", "pointsBySteamId", "Points", "points", "Balances", "balances")
OWNED_CONTAINER_KEYS = ("OwnedVehiclesBySteamId", "ownedVehiclesBySteamId", "OwnedVehicles", "ownedVehicles")
SHOP_CONTAINER_KEYS = ("Vehicles", "vehicles")


class SRVehicleHub(commands.Cog):
    """Discord vehicle shop hub for Swim Reapers AssettoServer."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=884421902303, force_registration=True)
        self.config.register_guild(
            delivery_points_paths=[],
            owned_vehicles_paths=[],
            vehicle_shop_path="",
            currency_name="$",
            public_profiles=True,
            allow_self_link=True,
            require_admin_to_link=False,
            purchase_dm_receipt=False,
        )
        self.config.register_user(steam_id="")
        self._file_lock = asyncio.Lock()

    # -------------------------
    # Basic file helpers
    # -------------------------

    @staticmethod
    def _resolve_path(path: str) -> Path:
        p = Path(str(path).strip().strip('"')).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        return p

    async def _read_json(self, path: str) -> Any:
        resolved = self._resolve_path(path)

        def _load() -> Any:
            with resolved.open("r", encoding="utf-8") as f:
                return json.load(f)

        return await asyncio.to_thread(_load)

    async def _write_json_atomic(self, path: str, data: Any) -> None:
        resolved = self._resolve_path(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)

        def _save() -> None:
            fd, temp_name = tempfile.mkstemp(prefix=resolved.name + ".", suffix=".tmp", dir=str(resolved.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                    f.write("\n")
                os.replace(temp_name, resolved)
            finally:
                if os.path.exists(temp_name):
                    try:
                        os.remove(temp_name)
                    except OSError:
                        pass

        await asyncio.to_thread(_save)

    @staticmethod
    def _looks_numeric(value: Any) -> bool:
        try:
            int(value)
            return True
        except Exception:
            return False

    @staticmethod
    def _looks_steam_id(value: str) -> bool:
        value = str(value).strip()
        return value.isdigit() and 15 <= len(value) <= 20

    @staticmethod
    def _format_money(amount: int, currency_name: str) -> str:
        if currency_name == "$":
            return f"${amount:,}"
        return f"{amount:,} {currency_name}"

    @staticmethod
    def _chunk_lines(lines: Iterable[str], max_len: int = 1800) -> List[str]:
        chunks: List[str] = []
        current = ""
        for line in lines:
            if len(current) + len(line) + 1 > max_len:
                if current:
                    chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)
        return chunks

    # -------------------------
    # JSON format adapters
    # -------------------------

    @staticmethod
    def _find_points_container(data: Any) -> Tuple[Optional[Dict[str, Any]], str]:
        if isinstance(data, dict):
            for key in POINTS_CONTAINER_KEYS:
                if isinstance(data.get(key), dict):
                    return data[key], key

            # Direct format: { "7656...": 123 }
            direct = all(SRVehicleHub._looks_steam_id(str(k)) and SRVehicleHub._looks_numeric(v) for k, v in data.items()) if data else False
            if direct:
                return data, "__direct__"

        return None, ""

    @staticmethod
    def _extract_points(data: Any) -> Dict[str, int]:
        container, _ = SRVehicleHub._find_points_container(data)
        if container is not None:
            return {
                str(k): int(v)
                for k, v in container.items()
                if SRVehicleHub._looks_steam_id(str(k)) and SRVehicleHub._looks_numeric(v)
            }

        if isinstance(data, list):
            result: Dict[str, int] = {}
            for item in data:
                if not isinstance(item, dict):
                    continue
                steam_id = item.get("SteamId") or item.get("steamId") or item.get("Guid") or item.get("guid")
                points = item.get("Points") or item.get("points") or item.get("Balance") or item.get("balance")
                if steam_id and SRVehicleHub._looks_numeric(points):
                    result[str(steam_id)] = int(points)
            return result

        return {}

    @staticmethod
    def _set_points_balance(data: Any, steam_id: str, new_balance: int) -> Any:
        container, container_key = SRVehicleHub._find_points_container(data)
        if container is not None:
            container[steam_id] = int(new_balance)
            return data

        if isinstance(data, dict):
            # Empty/new dict: use direct format.
            data[steam_id] = int(new_balance)
            return data

        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                existing = item.get("SteamId") or item.get("steamId") or item.get("Guid") or item.get("guid")
                if str(existing) == steam_id:
                    if "Points" in item:
                        item["Points"] = int(new_balance)
                    elif "points" in item:
                        item["points"] = int(new_balance)
                    elif "Balance" in item:
                        item["Balance"] = int(new_balance)
                    elif "balance" in item:
                        item["balance"] = int(new_balance)
                    else:
                        item["Points"] = int(new_balance)
                    return data

            data.append({"SteamId": steam_id, "Points": int(new_balance)})
            return data

        return {steam_id: int(new_balance)}

    @staticmethod
    def _coerce_vehicle_list(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(x) for x in value]
        if isinstance(value, tuple):
            return [str(x) for x in value]
        if isinstance(value, set):
            return [str(x) for x in value]
        if isinstance(value, dict):
            return [str(k) for k, owned in value.items() if bool(owned)]
        if isinstance(value, str):
            return [x.strip() for x in value.split(",") if x.strip()]
        return []

    @staticmethod
    def _find_owned_container(data: Any) -> Tuple[Optional[Dict[str, Any]], str]:
        if isinstance(data, dict):
            for key in OWNED_CONTAINER_KEYS:
                if isinstance(data.get(key), dict):
                    return data[key], key

            # Direct format: { "7656...": ["car"] }
            direct = all(SRVehicleHub._looks_steam_id(str(k)) for k in data.keys()) if data else False
            if direct:
                return data, "__direct__"

        return None, ""

    @staticmethod
    def _extract_owned(data: Any) -> Dict[str, List[str]]:
        container, _ = SRVehicleHub._find_owned_container(data)
        if container is not None:
            return {
                str(k): sorted({str(x).strip() for x in SRVehicleHub._coerce_vehicle_list(v) if str(x).strip()})
                for k, v in container.items()
                if SRVehicleHub._looks_steam_id(str(k))
            }

        if isinstance(data, list):
            result: Dict[str, List[str]] = {}
            for item in data:
                if not isinstance(item, dict):
                    continue
                steam_id = item.get("SteamId") or item.get("steamId") or item.get("Guid") or item.get("guid")
                vehicles = item.get("Vehicles") or item.get("vehicles") or item.get("OwnedVehicles") or item.get("ownedVehicles")
                if steam_id:
                    result[str(steam_id)] = sorted({str(x).strip() for x in SRVehicleHub._coerce_vehicle_list(vehicles) if str(x).strip()})
            return result

        return {}

    @staticmethod
    def _add_owned_vehicle(data: Any, steam_id: str, model: str) -> Any:
        model = str(model).strip()
        if not model:
            return data

        container, _ = SRVehicleHub._find_owned_container(data)
        if container is not None:
            vehicles = SRVehicleHub._coerce_vehicle_list(container.get(steam_id))
            normalized = sorted({str(v).strip() for v in vehicles if str(v).strip()} | {model}, key=str.lower)
            container[steam_id] = normalized
            return data

        if isinstance(data, dict):
            # VehicleOwnershipPlugin default format.
            data.setdefault("OwnedVehiclesBySteamId", {})
            data["OwnedVehiclesBySteamId"][steam_id] = [model]
            return data

        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                existing = item.get("SteamId") or item.get("steamId") or item.get("Guid") or item.get("guid")
                if str(existing) == steam_id:
                    key = "Vehicles" if "Vehicles" in item else "vehicles" if "vehicles" in item else "OwnedVehicles"
                    vehicles = SRVehicleHub._coerce_vehicle_list(item.get(key))
                    item[key] = sorted({str(v).strip() for v in vehicles if str(v).strip()} | {model}, key=str.lower)
                    return data
            data.append({"SteamId": steam_id, "Vehicles": [model]})
            return data

        return {"OwnedVehiclesBySteamId": {steam_id: [model]}}

    @staticmethod
    def _extract_shop(data: Any) -> Dict[str, Dict[str, Any]]:
        vehicles = None
        if isinstance(data, dict):
            for key in SHOP_CONTAINER_KEYS:
                if isinstance(data.get(key), dict):
                    vehicles = data[key]
                    break
            if vehicles is None:
                vehicles = data

        result: Dict[str, Dict[str, Any]] = {}

        if isinstance(vehicles, dict):
            for model, value in vehicles.items():
                if isinstance(value, dict):
                    display = value.get("DisplayName") or value.get("displayName") or value.get("Name") or value.get("name") or str(model)
                    price = value.get("Price") or value.get("price") or value.get("Cost") or value.get("cost") or 0
                else:
                    display = str(model)
                    price = value if SRVehicleHub._looks_numeric(value) else 0

                try:
                    price_int = int(price)
                except Exception:
                    price_int = 0

                result[str(model)] = {"display_name": str(display), "price": price_int}

        elif isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                model = item.get("Model") or item.get("model") or item.get("Id") or item.get("id")
                if not model:
                    continue
                display = item.get("DisplayName") or item.get("displayName") or item.get("Name") or item.get("name") or str(model)
                price = item.get("Price") or item.get("price") or item.get("Cost") or item.get("cost") or 0
                try:
                    price_int = int(price)
                except Exception:
                    price_int = 0
                result[str(model)] = {"display_name": str(display), "price": price_int}

        return result

    @staticmethod
    def _find_shop_item(shop: Dict[str, Dict[str, Any]], query: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        q = query.strip().lower()

        # Exact model.
        for model, item in shop.items():
            if model.lower() == q:
                return model, item

        # Exact display name.
        for model, item in shop.items():
            if str(item.get("display_name", "")).lower() == q:
                return model, item

        # Partial unique match.
        matches = []
        for model, item in shop.items():
            haystack = f"{model} {item.get('display_name', '')}".lower()
            if q in haystack:
                matches.append((model, item))

        if len(matches) == 1:
            return matches[0]

        return None

    # -------------------------
    # Config guards
    # -------------------------

    async def _is_adminish(self, ctx: commands.Context) -> bool:
        if await self.bot.is_admin(ctx.author):
            return True
        perms = ctx.channel.permissions_for(ctx.author)
        return bool(perms.manage_guild)

    async def _can_view_profiles(self, ctx: commands.Context) -> bool:
        if await self.config.guild(ctx.guild).public_profiles():
            return True
        return await self._is_adminish(ctx)

    async def _get_linked_steam_id(self, user: discord.User) -> str:
        return str(await self.config.user(user).steam_id()).strip()

    # -------------------------
    # Multi-file economy/ownership ops
    # -------------------------

    async def _load_points_files(self, paths: List[str]) -> List[Tuple[str, Any, Dict[str, int]]]:
        loaded = []
        for path in paths:
            raw = await self._read_json(path)
            loaded.append((path, raw, self._extract_points(raw)))
        return loaded

    async def _load_owned_files(self, paths: List[str]) -> List[Tuple[str, Any, Dict[str, List[str]]]]:
        loaded = []
        for path in paths:
            raw = await self._read_json(path)
            loaded.append((path, raw, self._extract_owned(raw)))
        return loaded

    async def _purchase_vehicle(self, guild: discord.Guild, steam_id: str, model: str, price: int) -> Tuple[int, int, List[str]]:
        """Atomically deduct points and add ownership to all configured ownership files.

        Returns: old_total, new_total, updated_owned_paths
        """
        async with self._file_lock:
            cfg = await self.config.guild(guild).all()
            points_paths = list(cfg["delivery_points_paths"])
            owned_paths = list(cfg["owned_vehicles_paths"])

            if not points_paths:
                raise RuntimeError("No delivery_points.json paths configured.")
            if not owned_paths:
                raise RuntimeError("No owned_vehicles.json paths configured.")

            points_files = await self._load_points_files(points_paths)
            owned_files = await self._load_owned_files(owned_paths)

            # If owned in any configured server, treat as already owned.
            for _, _, owned in owned_files:
                if model in set(owned.get(steam_id, [])):
                    raise ValueError("already_owned")

            old_total = sum(points.get(steam_id, 0) for _, _, points in points_files)
            if old_total < price:
                raise ValueError("insufficient_funds")

            remaining = price
            updated_point_files: List[Tuple[str, Any]] = []

            # Deduct from the first file with enough balance if possible, otherwise spread across files.
            enough_single = None
            for i, (path, raw, points) in enumerate(points_files):
                balance = points.get(steam_id, 0)
                if balance >= price:
                    enough_single = i
                    break

            if enough_single is not None:
                path, raw, points = points_files[enough_single]
                new_balance = points.get(steam_id, 0) - price
                updated_point_files.append((path, self._set_points_balance(raw, steam_id, new_balance)))
            else:
                for path, raw, points in points_files:
                    if remaining <= 0:
                        break
                    balance = points.get(steam_id, 0)
                    if balance <= 0:
                        continue
                    take = min(balance, remaining)
                    remaining -= take
                    updated_point_files.append((path, self._set_points_balance(raw, steam_id, balance - take)))

            if remaining > 0:
                raise ValueError("insufficient_funds")

            updated_owned_files: List[Tuple[str, Any]] = []
            for path, raw, _ in owned_files:
                updated_owned_files.append((path, self._add_owned_vehicle(raw, steam_id, model)))

            # Write after all validations succeed.
            for path, raw in updated_point_files:
                await self._write_json_atomic(path, raw)

            for path, raw in updated_owned_files:
                await self._write_json_atomic(path, raw)

            new_total = old_total - price
            return old_total, new_total, [path for path, _ in updated_owned_files]

    # -------------------------
    # Commands
    # -------------------------

    @commands.group(name="srhub")
    async def srhub(self, ctx: commands.Context):
        """Swim Reapers vehicle shop hub."""
        pass

    @srhub.group(name="config")
    @checks.admin_or_permissions(manage_guild=True)
    async def config_group(self, ctx: commands.Context):
        """Configure SRVehicleHub."""
        pass

    @config_group.command(name="addpoints")
    async def addpoints(self, ctx: commands.Context, *, path: str):
        """Add a delivery_points.json path. Repeat once per server or use one shared file."""
        paths = list(await self.config.guild(ctx.guild).delivery_points_paths())
        if path not in paths:
            paths.append(path)
        await self.config.guild(ctx.guild).delivery_points_paths.set(paths)
        await ctx.tick()

    @config_group.command(name="removepoints")
    async def removepoints(self, ctx: commands.Context, *, path: str):
        """Remove a delivery_points.json path."""
        paths = [p for p in await self.config.guild(ctx.guild).delivery_points_paths() if p != path]
        await self.config.guild(ctx.guild).delivery_points_paths.set(paths)
        await ctx.tick()

    @config_group.command(name="addowned")
    async def addowned(self, ctx: commands.Context, *, path: str):
        """Add an owned_vehicles.json path. Add every server path to mirror ownership across servers."""
        paths = list(await self.config.guild(ctx.guild).owned_vehicles_paths())
        if path not in paths:
            paths.append(path)
        await self.config.guild(ctx.guild).owned_vehicles_paths.set(paths)
        await ctx.tick()

    @config_group.command(name="removeowned")
    async def removeowned(self, ctx: commands.Context, *, path: str):
        """Remove an owned_vehicles.json path."""
        paths = [p for p in await self.config.guild(ctx.guild).owned_vehicles_paths() if p != path]
        await self.config.guild(ctx.guild).owned_vehicles_paths.set(paths)
        await ctx.tick()

    @config_group.command(name="setshop")
    async def setshop(self, ctx: commands.Context, *, path: str):
        """Set vehicle_shop.json path."""
        await self.config.guild(ctx.guild).vehicle_shop_path.set(path)
        await ctx.tick()

    @config_group.command(name="currency")
    async def currency(self, ctx: commands.Context, *, currency_name: str):
        """Set currency display, default is $."""
        await self.config.guild(ctx.guild).currency_name.set(currency_name.strip() or "$")
        await ctx.tick()

    @config_group.command(name="selflink")
    async def selflink(self, ctx: commands.Context, enabled: bool):
        """Allow users to link their own SteamID."""
        await self.config.guild(ctx.guild).allow_self_link.set(enabled)
        await ctx.tick()

    @config_group.command(name="adminlink")
    async def adminlink(self, ctx: commands.Context, enabled: bool):
        """Require admins to link SteamIDs for users."""
        await self.config.guild(ctx.guild).require_admin_to_link.set(enabled)
        await ctx.tick()

    @config_group.command(name="publicprofiles")
    async def publicprofiles(self, ctx: commands.Context, enabled: bool):
        """Allow regular users to view balance/profile/shop commands."""
        await self.config.guild(ctx.guild).public_profiles.set(enabled)
        await ctx.tick()

    @config_group.command(name="dmreceipt")
    async def dmreceipt(self, ctx: commands.Context, enabled: bool):
        """DM users purchase receipts when possible."""
        await self.config.guild(ctx.guild).purchase_dm_receipt.set(enabled)
        await ctx.tick()

    @srhub.command(name="paths")
    @checks.admin_or_permissions(manage_guild=True)
    async def paths(self, ctx: commands.Context):
        """Show configured file paths."""
        cfg = await self.config.guild(ctx.guild).all()
        embed = discord.Embed(title="SRVehicleHub Paths", color=discord.Color.green())
        embed.add_field(name="delivery_points.json paths", value="\n".join(f"`{p}`" for p in cfg["delivery_points_paths"]) or "not set", inline=False)
        embed.add_field(name="owned_vehicles.json paths", value="\n".join(f"`{p}`" for p in cfg["owned_vehicles_paths"]) or "not set", inline=False)
        embed.add_field(name="vehicle_shop.json", value=f"`{cfg['vehicle_shop_path']}`" if cfg["vehicle_shop_path"] else "not set", inline=False)
        embed.add_field(name="Currency", value=cfg["currency_name"], inline=True)
        embed.add_field(name="Public profiles", value=str(cfg["public_profiles"]), inline=True)
        embed.add_field(name="Self-link", value=str(cfg["allow_self_link"]), inline=True)
        await ctx.send(embed=embed)

    @srhub.command(name="status")
    @checks.admin_or_permissions(manage_guild=True)
    async def status(self, ctx: commands.Context):
        """Check configured files and parsed counts."""
        cfg = await self.config.guild(ctx.guild).all()
        lines: List[str] = []

        for path in cfg["delivery_points_paths"]:
            try:
                raw = await self._read_json(path)
                points = self._extract_points(raw)
                lines.append(f"✅ points `{path}` — {len(points):,} SteamIDs")
            except Exception as ex:
                lines.append(f"❌ points `{path}` — `{type(ex).__name__}: {ex}`")

        for path in cfg["owned_vehicles_paths"]:
            try:
                raw = await self._read_json(path)
                owned = self._extract_owned(raw)
                total_vehicles = sum(len(v) for v in owned.values())
                lines.append(f"✅ owned `{path}` — {len(owned):,} SteamIDs / {total_vehicles:,} vehicles")
            except Exception as ex:
                lines.append(f"❌ owned `{path}` — `{type(ex).__name__}: {ex}`")

        try:
            raw = await self._read_json(cfg["vehicle_shop_path"])
            shop = self._extract_shop(raw)
            lines.append(f"✅ shop `{cfg['vehicle_shop_path']}` — {len(shop):,} vehicles")
        except Exception as ex:
            lines.append(f"❌ shop `{cfg['vehicle_shop_path'] or 'not set'}` — `{type(ex).__name__}: {ex}`")

        if not lines:
            lines.append("No paths configured.")

        for chunk in self._chunk_lines(lines):
            await ctx.send(chunk)

    @srhub.command(name="link")
    async def link(self, ctx: commands.Context, steam_id: str):
        """Link your Discord account to your SteamID64."""
        cfg = await self.config.guild(ctx.guild).all()

        if cfg["require_admin_to_link"] or not cfg["allow_self_link"]:
            if not await self._is_adminish(ctx):
                return await ctx.send("Self-linking is disabled. Ask staff to link your SteamID.")

        steam_id = steam_id.strip()
        if not self._looks_steam_id(steam_id):
            return await ctx.send("That does not look like a valid SteamID64.")

        await self.config.user(ctx.author).steam_id.set(steam_id)
        await ctx.send(f"{ctx.author.mention}, linked to SteamID `{steam_id}`.")

    @srhub.command(name="linkuser")
    @checks.admin_or_permissions(manage_guild=True)
    async def linkuser(self, ctx: commands.Context, member: discord.Member, steam_id: str):
        """Staff command: link a Discord member to a SteamID64."""
        steam_id = steam_id.strip()
        if not self._looks_steam_id(steam_id):
            return await ctx.send("That does not look like a valid SteamID64.")

        await self.config.user(member).steam_id.set(steam_id)
        await ctx.send(f"Linked {member.mention} to SteamID `{steam_id}`.")

    @srhub.command(name="unlink")
    async def unlink(self, ctx: commands.Context):
        """Unlink your SteamID."""
        await self.config.user(ctx.author).steam_id.set("")
        await ctx.tick()

    @srhub.command(name="mysteam")
    async def mysteam(self, ctx: commands.Context):
        """Show your linked SteamID."""
        steam_id = await self._get_linked_steam_id(ctx.author)
        if not steam_id:
            return await ctx.send("You do not have a SteamID linked. Use `[p]srhub link <SteamID64>`.")
        await ctx.send(f"{ctx.author.mention}, your linked SteamID is `{steam_id}`.")

    @srhub.command(name="balance")
    async def balance(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """Show your delivery balance, or another member's if staff."""
        target = member or ctx.author
        if target != ctx.author and not await self._is_adminish(ctx):
            return await ctx.send("Only staff can check another member's balance.")
        if not await self._can_view_profiles(ctx):
            return await ctx.send("You do not have permission to use this command.")

        steam_id = await self._get_linked_steam_id(target)
        if not steam_id:
            return await ctx.send(f"{target.display_name} does not have a linked SteamID.")

        cfg = await self.config.guild(ctx.guild).all()
        total = 0
        try:
            for path in cfg["delivery_points_paths"]:
                total += self._extract_points(await self._read_json(path)).get(steam_id, 0)
        except Exception as ex:
            return await ctx.send(f"Could not read delivery points: `{type(ex).__name__}: {ex}`")

        await ctx.send(f"{target.mention} balance: **{self._format_money(total, cfg['currency_name'])}**")

    @srhub.command(name="garage")
    async def garage(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """Show owned vehicles."""
        target = member or ctx.author
        if target != ctx.author and not await self._is_adminish(ctx):
            return await ctx.send("Only staff can check another member's garage.")
        if not await self._can_view_profiles(ctx):
            return await ctx.send("You do not have permission to use this command.")

        steam_id = await self._get_linked_steam_id(target)
        if not steam_id:
            return await ctx.send(f"{target.display_name} does not have a linked SteamID.")

        cfg = await self.config.guild(ctx.guild).all()
        try:
            shop = self._extract_shop(await self._read_json(cfg["vehicle_shop_path"]))
            owned_all: set[str] = set()
            for path in cfg["owned_vehicles_paths"]:
                owned_all.update(self._extract_owned(await self._read_json(path)).get(steam_id, []))
        except Exception as ex:
            return await ctx.send(f"Could not read files: `{type(ex).__name__}: {ex}`")

        if not owned_all:
            return await ctx.send(f"{target.mention} owns no vehicles yet.")

        lines = []
        for model in sorted(owned_all, key=str.lower):
            display = shop.get(model, {}).get("display_name", model)
            lines.append(f"• **{display}** (`{model}`)")

        for chunk in self._chunk_lines(lines):
            await ctx.send(chunk)

    @srhub.command(name="profile")
    async def profile(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """Show linked SteamID, balance, and garage summary."""
        target = member or ctx.author
        if target != ctx.author and not await self._is_adminish(ctx):
            return await ctx.send("Only staff can check another member's profile.")
        if not await self._can_view_profiles(ctx):
            return await ctx.send("You do not have permission to use this command.")

        steam_id = await self._get_linked_steam_id(target)
        if not steam_id:
            return await ctx.send(f"{target.display_name} does not have a linked SteamID.")

        cfg = await self.config.guild(ctx.guild).all()

        try:
            shop = self._extract_shop(await self._read_json(cfg["vehicle_shop_path"]))
            balance = 0
            for path in cfg["delivery_points_paths"]:
                balance += self._extract_points(await self._read_json(path)).get(steam_id, 0)

            owned_all: set[str] = set()
            for path in cfg["owned_vehicles_paths"]:
                owned_all.update(self._extract_owned(await self._read_json(path)).get(steam_id, []))
        except Exception as ex:
            return await ctx.send(f"Could not read files: `{type(ex).__name__}: {ex}`")

        embed = discord.Embed(title=f"{target.display_name}'s SR Profile", color=discord.Color.green())
        embed.add_field(name="SteamID64", value=f"`{steam_id}`", inline=False)
        embed.add_field(name="Balance", value=self._format_money(balance, cfg["currency_name"]), inline=True)
        embed.add_field(name="Vehicles owned", value=str(len(owned_all)), inline=True)

        if owned_all:
            lines = []
            for model in sorted(owned_all, key=str.lower)[:20]:
                display = shop.get(model, {}).get("display_name", model)
                lines.append(f"• {display}")
            if len(owned_all) > 20:
                lines.append(f"...and {len(owned_all) - 20} more")
            embed.add_field(name="Garage", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Garage", value="No vehicles owned.", inline=False)

        await ctx.send(embed=embed)

    @srhub.command(name="shop")
    async def shop(self, ctx: commands.Context, *, search: Optional[str] = None):
        """Show vehicle shop, optionally filtered by search text."""
        cfg = await self.config.guild(ctx.guild).all()

        try:
            shop = self._extract_shop(await self._read_json(cfg["vehicle_shop_path"]))
        except Exception as ex:
            return await ctx.send(f"Could not read vehicle shop: `{type(ex).__name__}: {ex}`")

        if not shop:
            return await ctx.send("Vehicle shop is empty or could not be parsed.")

        search_lower = search.lower() if search else None
        lines = []

        for model, item in sorted(shop.items(), key=lambda kv: (kv[1].get("price", 0), kv[1].get("display_name", kv[0]).lower())):
            display = item.get("display_name", model)
            price = int(item.get("price", 0))
            haystack = f"{model} {display}".lower()
            if search_lower and search_lower not in haystack:
                continue
            lines.append(f"• **{display}** — {self._format_money(price, cfg['currency_name'])} (`{model}`)")

        if not lines:
            return await ctx.send("No shop vehicles matched that search.")

        for chunk in self._chunk_lines(lines[:100]):
            await ctx.send(chunk)

    @srhub.command(name="buy")
    async def buy(self, ctx: commands.Context, *, car: str):
        """Buy a vehicle from vehicle_shop.json with your DeliveryPlugin points."""
        steam_id = await self._get_linked_steam_id(ctx.author)
        if not steam_id:
            return await ctx.send("You need to link your SteamID first: `[p]srhub link <SteamID64>`")

        cfg = await self.config.guild(ctx.guild).all()

        try:
            shop = self._extract_shop(await self._read_json(cfg["vehicle_shop_path"]))
        except Exception as ex:
            return await ctx.send(f"Could not read vehicle shop: `{type(ex).__name__}: {ex}`")

        found = self._find_shop_item(shop, car)
        if found is None:
            return await ctx.send("I could not find a unique matching vehicle. Use `[p]srhub shop <search>` and buy by exact model if needed.")

        model, item = found
        display = str(item.get("display_name", model))
        price = int(item.get("price", 0))

        if price <= 0:
            return await ctx.send(f"**{display}** has an invalid price in `vehicle_shop.json`.")

        try:
            old_total, new_total, updated_paths = await self._purchase_vehicle(ctx.guild, steam_id, model, price)
        except ValueError as ex:
            if str(ex) == "already_owned":
                return await ctx.send(f"{ctx.author.mention}, you already own **{display}**.")
            if str(ex) == "insufficient_funds":
                # Get current balance for useful reply.
                total = 0
                try:
                    for path in cfg["delivery_points_paths"]:
                        total += self._extract_points(await self._read_json(path)).get(steam_id, 0)
                except Exception:
                    pass
                return await ctx.send(
                    f"{ctx.author.mention}, you do not have enough delivery points for **{display}**. "
                    f"Price: **{self._format_money(price, cfg['currency_name'])}**. "
                    f"Balance: **{self._format_money(total, cfg['currency_name'])}**."
                )
            return await ctx.send(f"Purchase failed: `{ex}`")
        except Exception as ex:
            return await ctx.send(f"Purchase failed while reading/writing files: `{type(ex).__name__}: {ex}`")

        msg = (
            f"✅ {ctx.author.mention} bought **{display}** (`{model}`) for "
            f"**{self._format_money(price, cfg['currency_name'])}**.\n"
            f"Balance: **{self._format_money(old_total, cfg['currency_name'])}** → "
            f"**{self._format_money(new_total, cfg['currency_name'])}**\n"
            f"Ownership synced to **{len(updated_paths)}** server file(s)."
        )
        await ctx.send(msg)

        if cfg["purchase_dm_receipt"]:
            try:
                await ctx.author.send(msg)
            except discord.HTTPException:
                pass

    @srhub.command(name="grantcar")
    @checks.admin_or_permissions(manage_guild=True)
    async def grantcar(self, ctx: commands.Context, member: discord.Member, *, car: str):
        """Staff: grant a vehicle without charging points."""
        steam_id = await self._get_linked_steam_id(member)
        if not steam_id:
            return await ctx.send(f"{member.display_name} does not have a linked SteamID.")

        cfg = await self.config.guild(ctx.guild).all()
        try:
            shop = self._extract_shop(await self._read_json(cfg["vehicle_shop_path"]))
            found = self._find_shop_item(shop, car)
            model = found[0] if found else car.strip()
            display = found[1].get("display_name", model) if found else model

            async with self._file_lock:
                for path in cfg["owned_vehicles_paths"]:
                    raw = await self._read_json(path)
                    raw = self._add_owned_vehicle(raw, steam_id, model)
                    await self._write_json_atomic(path, raw)
        except Exception as ex:
            return await ctx.send(f"Grant failed: `{type(ex).__name__}: {ex}`")

        await ctx.send(f"Granted **{display}** (`{model}`) to {member.mention} on {len(cfg['owned_vehicles_paths'])} server file(s).")

    @srhub.command(name="top")
    async def top(self, ctx: commands.Context, count: int = 10):
        """Show top linked SteamID balances across configured delivery files."""
        if not await self._can_view_profiles(ctx):
            return await ctx.send("You do not have permission to use this command.")

        count = max(1, min(count, 25))
        cfg = await self.config.guild(ctx.guild).all()
        totals: Dict[str, int] = {}

        try:
            for path in cfg["delivery_points_paths"]:
                points = self._extract_points(await self._read_json(path))
                for steam_id, amount in points.items():
                    totals[steam_id] = totals.get(steam_id, 0) + amount
        except Exception as ex:
            return await ctx.send(f"Could not read delivery points: `{type(ex).__name__}: {ex}`")

        if not totals:
            return await ctx.send("No delivery balances found.")

        rows = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:count]
        lines = [
            f"`{i:02d}.` `{steam_id}` — **{self._format_money(balance, cfg['currency_name'])}**"
            for i, (steam_id, balance) in enumerate(rows, start=1)
        ]
        await ctx.send("\n".join(lines))
