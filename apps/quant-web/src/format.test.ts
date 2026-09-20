/**
 * 统一格式化工具单测（node:test + tsx，零新依赖）。
 *
 * 这些函数是全站展示口径的唯一实现，因此把"空值 → —"、"是否带正号"、
 * "日期两种上游格式"这几条易回归的约定锁进测试。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { fileBaseName, fmtDate, fmtDateTime, fmtInt, fmtMinute, fmtMoney, fmtNum, fmtNumTrim, fmtPct, fmtPercent, fmtPercentTrim, quoteUrl } from "./format";

const pad = (n: number) => String(n).padStart(2, "0");

test("空值/NaN 一律显示 —", () => {
  for (const value of [null, undefined, Number.NaN]) {
    assert.equal(fmtNum(value), "—");
    assert.equal(fmtNumTrim(value), "—");
    assert.equal(fmtPct(value), "—");
    assert.equal(fmtPercent(value), "—");
    assert.equal(fmtPercentTrim(value), "—");
    assert.equal(fmtInt(value), "—");
    assert.equal(fmtMoney(value), "—");
  }
});

test("fmtNum 默认 2 位小数，可指定精度", () => {
  assert.equal(fmtNum(1.005), "1.00");
  assert.equal(fmtNum(1.23456), "1.23");
  assert.equal(fmtNum(1.23456, 4), "1.2346");
  assert.equal(fmtNum(0), "0.00");
});

test("fmtNumTrim 裁剪尾随 0（DataCenter 因子分位口径）", () => {
  assert.equal(fmtNumTrim(1.5), "1.5");
  assert.equal(fmtNumTrim(1.234567, 4), "1.2346");
  assert.equal(fmtNumTrim(2), "2");
  assert.equal(fmtNumTrim(0.00001, 4), "0");
});

test("fmtPct 入参是比率且默认带正号", () => {
  assert.equal(fmtPct(0.1234), "+12.34%");
  assert.equal(fmtPct(-0.05), "-5.00%");
  assert.equal(fmtPct(0.05, 1), "+5.0%");
  assert.equal(fmtPct(0.987, 2, false), "98.70%");
});

test("fmtPercent 入参已经是百分数，不再乘 100", () => {
  assert.equal(fmtPercent(50), "50.00%");
  assert.equal(fmtPercent(12.345, 1), "12.3%");
});

test("fmtPercentTrim 裁剪尾随 0（DataCenter 数据集完整度口径）", () => {
  assert.equal(fmtPercentTrim(98.5), "98.5%");
  assert.equal(fmtPercentTrim(100), "100%");
  assert.equal(fmtPercentTrim(33.333), "33.33%");
});

test("fmtMoney 使用人民币千分位与 2 位小数", () => {
  assert.equal(fmtMoney(1234567.5), "¥1,234,567.50");
});

test("fmtInt 四舍五入后加千分位", () => {
  assert.equal(fmtInt(1234.6), "1,235");
  assert.equal(fmtInt(0), "0");
});

test("fmtDate 同时兼容紧凑日期与 ISO 时间戳", () => {
  assert.equal(fmtDate("20260911"), "2026-09-11");
  assert.equal(fmtDate("2026-09-11T08:14:39.475Z"), "2026-09-11");
  assert.equal(fmtDate(null), "—");
});

test("fmtDateTime / fmtMinute 按本地时区渲染 UTC 时间戳（不能用字符串切片）", () => {
  const iso = "2026-09-11T08:14:39.475Z";
  const d = new Date(iso);
  const localDate = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  assert.equal(fmtDateTime(iso), `${localDate} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`);
  assert.equal(fmtMinute(iso), `${localDate} ${pad(d.getHours())}:${pad(d.getMinutes())}`);
  assert.equal(fmtDateTime(null), "—");
  assert.equal(fmtMinute(null), "—");
});

test("quoteUrl 按交易所前缀生成东财链接", () => {
  assert.equal(quoteUrl("600000.SH"), "https://quote.eastmoney.com/sh600000.html");
  assert.equal(quoteUrl("000001.SZ"), "https://quote.eastmoney.com/sz000001.html");
});

test("fileBaseName 取 Windows 路径末段", () => {
  assert.equal(fileBaseName("D:\\artifacts\\stock-ml\\stock-wf-model.txt"), "stock-wf-model.txt");
  assert.equal(fileBaseName(null), "—");
});
