 (cd "$(git rev-parse --show-toplevel)" && git apply --3way <<'EOF' 
diff --git a/bot.py b/bot.py
new file mode 100644
index 0000000000000000000000000000000000000000..800c071c17e23017168ecc3700f4cf6b23094e4c
--- /dev/null
+++ b/bot.py
@@ -0,0 +1,298 @@
+import json
+import logging
+import os
+from dataclasses import dataclass
+from pathlib import Path
+from typing import Iterable, List, Optional
+
+from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
+from telegram.ext import (
+    ApplicationBuilder,
+    CallbackQueryHandler,
+    CommandHandler,
+    ContextTypes,
+)
+
+
+@dataclass
+class BotConfig:
+    token: str
+    admin_ids: List[int]
+    state_path: Path
+    master_id: Optional[int]
+
+    @classmethod
+    def from_env(cls) -> "BotConfig":
+        token = os.getenv("BOT_TOKEN")
+        admin_value = os.getenv("BOT_ADMIN_IDS", "")
+        admin_ids = cls._parse_ids(admin_value)
+        state_path = Path(os.getenv("BOT_STATE_PATH", "data/state.json"))
+        master_id = cls._parse_master(os.getenv("BOT_MASTER_ID"))
+        return cls(
+            token=token,
+            admin_ids=admin_ids,
+            state_path=state_path,
+            master_id=master_id,
+        )
+
+    @staticmethod
+    def _parse_ids(value: str) -> List[int]:
+        ids: List[int] = []
+        for part in value.split(","):
+            trimmed = part.strip()
+            if trimmed:
+                try:
+                    ids.append(int(trimmed))
+                except ValueError:
+                    logging.warning("Ignoring admin id '%s' because it is not an integer", trimmed)
+        return ids
+
+    @staticmethod
+    def _parse_master(value: Optional[str]) -> Optional[int]:
+        if value is None or value.strip() == "":
+            return None
+        try:
+            return int(value.strip())
+        except ValueError:
+            logging.warning("BOT_MASTER_ID must be integer. Ignoring provided value '%s'", value)
+            return None
+
+
+class StateStore:
+    def __init__(self, path: Path):
+        self._path = path
+        self._path.parent.mkdir(parents=True, exist_ok=True)
+        self._state = self._load()
+
+    def _load(self) -> dict:
+        if not self._path.exists():
+            return {
+                "status": "closed",  # closed | long | short
+                "stop_loss": None,
+                "updated_by": None,
+                "last_action": "initial",
+            }
+        try:
+            with self._path.open("r", encoding="utf-8") as file:
+                return json.load(file)
+        except json.JSONDecodeError:
+            logging.warning("State file %s was invalid JSON. Resetting state.", self._path)
+            return {
+                "status": "closed",
+                "stop_loss": None,
+                "updated_by": None,
+                "last_action": "reset",
+            }
+
+    def _save(self) -> None:
+        with self._path.open("w", encoding="utf-8") as file:
+            json.dump(self._state, file, ensure_ascii=False, indent=2)
+
+    def open_position(self, direction: str, updated_by: int) -> dict:
+        self._state.update(
+            {
+                "status": direction,
+                "updated_by": updated_by,
+                "last_action": f"open_{direction}",
+            }
+        )
+        self._save()
+        return self._state
+
+    def close_position(self, updated_by: int) -> dict:
+        self._state.update(
+            {
+                "status": "closed",
+                "updated_by": updated_by,
+                "last_action": "close",
+            }
+        )
+        self._save()
+        return self._state
+
+    def update_stop_loss(self, value: Optional[float], updated_by: int) -> dict:
+        self._state.update(
+            {
+                "stop_loss": value,
+                "updated_by": updated_by,
+                "last_action": "stop_loss" if value is not None else "clear_stop_loss",
+            }
+        )
+        self._save()
+        return self._state
+
+    def data(self) -> dict:
+        return self._state
+
+
+def build_keyboard(is_master: bool) -> InlineKeyboardMarkup:
+    rows = []
+    if is_master:
+        rows.append(
+            [
+                InlineKeyboardButton(
+                    text="📈 Открыть лонг", callback_data="open_long"
+                ),
+                InlineKeyboardButton(
+                    text="📉 Открыть шорт", callback_data="open_short"
+                ),
+            ]
+        )
+        rows.append(
+            [
+                InlineKeyboardButton(
+                    text="❌ Закрыть позицию", callback_data="close"
+                )
+            ]
+        )
+    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh")])
+    return InlineKeyboardMarkup(rows)
+
+
+def render_state_message(state: dict, user_is_admin: bool, is_master: bool) -> str:
+    status_map = {"closed": "Закрыто", "long": "Открыт лонг", "short": "Открыт шорт"}
+    status = status_map.get(state.get("status"), "Неизвестно")
+    updated_by = state.get("updated_by")
+    stop_loss = state.get("stop_loss")
+    lines = [
+        "Главное меню" if user_is_admin else "Меню сигналов",
+        f"Статус позиции: {status}",
+    ]
+    if stop_loss is not None:
+        lines.append(f"Стоп-лосс: {stop_loss}")
+    else:
+        lines.append("Стоп-лосс: не задан")
+    if updated_by is not None:
+        lines.append(f"Последнее действие от пользователя {updated_by}")
+    lines.append(
+        "Только главный аккаунт задаёт сигналы, остальные повторяют открытие/закрытие и стоп-лосс."
+    )
+    if is_master:
+        lines.append("Используйте кнопки или команду /stoploss <цена>, чтобы обновить стоп-лосс.")
+    else:
+        lines.append("Получайте сигналы и повторяйте их на своих аккаунтах.")
+    return "\n".join(lines)
+
+
+def is_admin(user_id: Optional[int], admins: Iterable[int]) -> bool:
+    return user_id is not None and user_id in admins
+
+
+def is_master(user_id: Optional[int], master_id: Optional[int]) -> bool:
+    if master_id is None:
+        return True
+    return user_id is not None and user_id == master_id
+
+
+def ensure_token(token: Optional[str]) -> str:
+    if not token:
+        raise RuntimeError("BOT_TOKEN is required to start the bot")
+    return token
+
+
+async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
+    user_id = update.effective_user.id if update.effective_user else None
+    store: StateStore = context.application.bot_data["store"]
+    config: BotConfig = context.application.bot_data["config"]
+    await update.message.reply_text(
+        render_state_message(
+            store.data(), is_admin(user_id, config.admin_ids), is_master(user_id, config.master_id)
+        ),
+        reply_markup=build_keyboard(is_master(user_id, config.master_id)),
+    )
+
+
+async def handle_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
+    query = update.callback_query
+    if not query:
+        return
+
+    await query.answer()
+    action = query.data
+    user_id = query.from_user.id if query.from_user else 0
+    store: StateStore = context.application.bot_data["store"]
+    config: BotConfig = context.application.bot_data["config"]
+
+    master = is_master(user_id, config.master_id)
+    if action in {"open_long", "open_short", "close"} and not master:
+        await query.answer("Только главный аккаунт может менять сигналы.", show_alert=True)
+        state = store.data()
+    elif action == "open_long":
+        state = store.open_position("long", user_id)
+    elif action == "open_short":
+        state = store.open_position("short", user_id)
+    elif action == "close":
+        state = store.close_position(user_id)
+    else:
+        state = store.data()
+
+    await query.edit_message_text(
+        render_state_message(
+            state, is_admin(user_id, config.admin_ids), master
+        ),
+        reply_markup=build_keyboard(master),
+    )
+
+
+async def handle_stop_loss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
+    user_id = update.effective_user.id if update.effective_user else None
+    config: BotConfig = context.application.bot_data["config"]
+    if not is_master(user_id, config.master_id):
+        await update.message.reply_text("Только главный аккаунт может менять стоп-лосс.")
+        return
+
+    args = context.args
+    if not args:
+        await update.message.reply_text(
+            "Укажите цену стоп-лосса: /stoploss <цена>. Для сброса используйте /stoploss clear"
+        )
+        return
+
+    value_arg = args[0]
+    store: StateStore = context.application.bot_data["store"]
+
+    if value_arg.lower() in {"clear", "none", "reset"}:
+        state = store.update_stop_loss(None, user_id)
+        await update.message.reply_text(
+            render_state_message(state, is_admin(user_id, config.admin_ids), True)
+        )
+        return
+
+    try:
+        value = float(value_arg.replace(",", "."))
+    except ValueError:
+        await update.message.reply_text("Не удалось прочитать цену. Пример: /stoploss 1234.5")
+        return
+
+    state = store.update_stop_loss(value, user_id)
+    await update.message.reply_text(
+        render_state_message(state, is_admin(user_id, config.admin_ids), True)
+    )
+
+
+def main() -> None:
+    logging.basicConfig(level=logging.INFO)
+
+    config = BotConfig.from_env()
+    token = ensure_token(config.token)
+    store = StateStore(config.state_path)
+
+    application = ApplicationBuilder().token(token).build()
+    application.bot_data["store"] = store
+    application.bot_data["config"] = config
+
+    application.add_handler(CommandHandler("start", start))
+    application.add_handler(CommandHandler("stoploss", handle_stop_loss))
+    application.add_handler(CallbackQueryHandler(handle_toggle))
+
+    if config.master_id is None:
+        logging.warning(
+            "BOT_MASTER_ID не задан. Команды трейдинга будут доступны любому пользователю."
+        )
+
+    logging.info("Bot is starting with state file at %s", config.state_path)
+    application.run_polling(allowed_updates=["message", "callback_query"])
+
+
+if __name__ == "__main__":
+    main()
 
EOF
)
