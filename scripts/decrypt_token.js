#!/usr/bin/env node
/*
 * 从 WorkBuddy 本机登录态导出 accessToken。
 *
 * 必须用 WorkBuddy 自带的 Electron 以 ELECTRON_RUN_AS_NODE=1 运行：
 *   ELECTRON_RUN_AS_NODE=1 /path/to/workbuddy scripts/decrypt_token.js --json
 *
 * 登录态文件由 WorkBuddy 用 electron_browser_workbuddy_storage 的静态钥加密；
 * 纯 Node 无法解密，所以这里复用 WorkBuddy 的 credential-protection 模块。
 */
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");

function fail(message) {
  process.stderr.write(`ERROR: ${message}\n`);
  process.exit(1);
}

function firstExisting(paths) {
  for (const candidate of paths) {
    if (candidate && fs.existsSync(candidate)) return candidate;
  }
  return "";
}

function authFileCandidates() {
  const home = os.homedir();
  const rel = path.join("CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info");
  if (process.platform === "darwin") {
    return [path.join(home, "Library", "Application Support", rel)];
  }
  if (process.platform === "win32") {
    const local = process.env.LOCALAPPDATA || "";
    const roaming = process.env.APPDATA || "";
    return [path.join(local, rel), path.join(roaming, rel)];
  }
  const xdgData = process.env.XDG_DATA_HOME || path.join(home, ".local", "share");
  const xdgConfig = process.env.XDG_CONFIG_HOME || path.join(home, ".config");
  return [path.join(xdgData, rel), path.join(xdgConfig, rel)];
}

function asarCandidates() {
  const home = os.homedir();
  if (process.platform === "darwin") {
    return [
      "/Applications/WorkBuddy.app/Contents/Resources/app.asar",
      path.join(home, "Applications/WorkBuddy.app/Contents/Resources/app.asar"),
    ];
  }
  if (process.platform === "win32") {
    const local = process.env.LOCALAPPDATA || "";
    const programFiles = process.env.PROGRAMFILES || "";
    return [
      path.join(local, "Programs", "WorkBuddy", "resources", "app.asar"),
      path.join(programFiles, "WorkBuddy", "resources", "app.asar"),
    ];
  }
  return [
    "/opt/workbuddy/resources/app.asar",
    path.join(home, ".local", "share", "workbuddy", "resources", "app.asar"),
  ];
}

function loadBinding() {
  try {
    return process._linkedBinding("electron_browser_workbuddy_storage");
  } catch (error) {
    fail(
      "当前解释器不是 WorkBuddy 自带的 Electron。请用 ELECTRON_RUN_AS_NODE=1 运行 WorkBuddy 二进制，或直接执行 scripts/refresh_token.py。",
    );
  }
}

function decodeWithStorage(asarPath, authPath) {
  const envelope = JSON.parse(fs.readFileSync(authPath, "utf8"));
  if (typeof envelope?.auth?.accessToken === "string") {
    return envelope;
  }
  if (!asarPath) {
    fail("登录态是加密的，但找不到 WorkBuddy 的 app.asar，请设置 WB_ASAR");
  }

  const binding = loadBinding();
  const raw = binding.loggerGet();
  const keyPayload = typeof raw === "string" ? JSON.parse(raw) : raw;
  if (!keyPayload || typeof keyPayload.atRestSecretKey !== "string") {
    fail("WorkBuddy 未返回可用的静态密钥");
  }

  const key = crypto.createHash("sha256").update(keyPayload.atRestSecretKey, "utf8").digest();
  const keyId = crypto.createHash("sha256").update(key).digest("hex").slice(0, 16);

  const protectionPath = path.join(asarPath, "main", "credential-protection.js");
  if (!fs.existsSync(protectionPath)) {
    fail(`找不到 credential-protection 模块: ${protectionPath}`);
  }
  const protection = require(protectionPath);
  if (typeof protection.init_credential_protection === "function") {
    protection.init_credential_protection();
  }
  protection.configureCredentialProtection({
    policy: "fields",
    symmetricKey: { key, keyId },
  });

  const codec = protection.createCredentialFieldCodec({
    purpose: "workbuddy-auth-session",
    resourceId: `auth/${path.basename(authPath)}`,
  });
  const decoded = codec.decode(envelope, protection.AUTH_CREDENTIAL_FIELDS);
  return decoded.value;
}

function main() {
  const asarPath = process.env.WB_ASAR || firstExisting(asarCandidates());
  const authPath = process.env.WB_AUTH || firstExisting(authFileCandidates());

  if (!authPath) {
    fail("找不到 WorkBuddy 登录态文件，请先在 WorkBuddy 中登录");
  }

  const session = decodeWithStorage(asarPath, authPath);
  const auth = session?.auth || {};
  const account = session?.account || {};
  if (!auth.accessToken) {
    fail("登录态中没有 accessToken");
  }

  const result = {
    accessToken: auth.accessToken,
    uid: account.uid || "",
    nickname: account.nickname || "",
    domain: auth.domain || "",
    enterpriseId: account.enterpriseId || "",
    expiresAt: auth.expiresAt || 0,
    refreshToken: auth.refreshToken || "",
  };

  if (process.argv.includes("--json")) {
    process.stdout.write(JSON.stringify(result) + "\n");
    return;
  }

  process.stdout.write(`DECRYPT_RESULT:${result.accessToken}\n`);
  process.stdout.write(`ACCOUNT_UID:${result.uid}\n`);
  process.stdout.write(`AUTH_DOMAIN:${result.domain}\n`);
  process.stdout.write(`ENTERPRISE_ID:${result.enterpriseId}\n`);
}

main();
