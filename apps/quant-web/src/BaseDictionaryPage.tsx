import { useEffect, useState } from "react";
import {
  Badge,
  Button,
  Form,
  Input,
  InputNumber,
  Listy,
  message,
  Modal,
  Popconfirm,
  Space,
  Switch,
  Tag,
} from "antd";
import { PlusOutlined } from "@ant-design/icons";
import {
  PageContainer,
  ProCard,
  ProTable,
  type ProColumns,
} from "@ant-design/pro-components";
import {
  settingsApi,
  type DictionaryItemDto,
  type DictionaryTypeDto,
} from "./api";
type Editor =
  | { kind: "types"; item: Partial<DictionaryTypeDto> }
  | { kind: "items"; item: Partial<DictionaryItemDto> };
export default function BaseDictionaryPage({
  onBusiness,
}: {
  onBusiness: () => void;
}) {
  const [types, setTypes] = useState<DictionaryTypeDto[]>([]),
    [items, setItems] = useState<DictionaryItemDto[]>([]),
    [selected, setSelected] = useState(""),
    [editor, setEditor] = useState<Editor | null>(null),
    [form] = Form.useForm();
  const load = () =>
    settingsApi
      .dictionary()
      .then((x) => {
        setTypes(x.types);
        setItems(x.items);
        setSelected((v) => v || x.types[0]?.code || "");
      })
      .catch((e) => message.error(String(e)));
  useEffect(() => {
    void load();
  }, []);
  const open = (next: Editor) => {
    setEditor(next);
    form.setFieldsValue(next.item);
  };
  const save = async () => {
    const value = await form.validateFields();
    await settingsApi.saveBaseDictionary(editor!.kind, value);
    message.success("基础字典已保存");
    setEditor(null);
    void load();
  };
  const remove = async (
    kind: "types" | "items",
    key: Record<string, string>,
  ) => {
    try {
      await settingsApi.deleteBaseDictionary(kind, key);
      message.success("已删除");
      void load();
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
    }
  };
  const current = types.find((x) => x.code === selected),
    rows = items.filter((x) => x.dictionaryCode === selected);
  const columns: ProColumns<DictionaryItemDto>[] = [
    {
      title: "编码",
      dataIndex: "itemCode",
      render: (v) => <Tag>{String(v)}</Tag>,
    },
    { title: "显示名称", dataIndex: "itemName" },
    { title: "排序", dataIndex: "sortOrder" },
    {
      title: "状态",
      render: (_, r) => (
        <Badge
          status={r.enabled ? "success" : "default"}
          text={r.enabled ? "启用" : "停用"}
        />
      ),
    },
    {
      title: "属性",
      render: (_, r) =>
        r.isSystem ? <Tag color="blue">系统项</Tag> : <Tag>自定义</Tag>,
    },
    { title: "说明", dataIndex: "description" },
    {
      title: "操作",
      valueType: "option",
      render: (_, r) => (
        <>
          <Button type="link" onClick={() => open({ kind: "items", item: r })}>
            编辑
          </Button>
          <Popconfirm
            title={r.isSystem ? "系统项不能删除，可编辑为停用" : "确认删除？"}
            disabled={r.isSystem}
            onConfirm={() =>
              void remove("items", {
                dictionaryCode: r.dictionaryCode,
                itemCode: r.itemCode,
              })
            }
          >
            <Button type="link" danger disabled={r.isSystem}>
              删除
            </Button>
          </Popconfirm>
        </>
      ),
    },
  ];
  return (
    <PageContainer
      title="基础字典"
      subTitle="统一维护分类、频率、字段类型和计量单位等稳定编码"
      extra={<Button onClick={onBusiness}>返回业务字典</Button>}
    >
      <div className="dictionary-layout">
        <ProCard
          className="dataset-master"
          title="字典类型"
          extra={
            <Button
              type="text"
              icon={<PlusOutlined />}
              onClick={() =>
                open({
                  kind: "types",
                  item: { code: "", name: "", enabled: true, isSystem: false },
                })
              }
            />
          }
        >
          <Listy
            items={types}
            rowKey="code"
            itemRender={(type) => (
              <div
                className={
                  selected === type.code ? "dataset-row selected" : "dataset-row"
                }
                onClick={() => setSelected(type.code)}
              >
                <div className="dataset-row-main" title={type.code}>
                  <b>{type.name}</b>
                  <span>{type.code}</span>
                  <small>
                    {
                      items.filter((x) => x.dictionaryCode === type.code).length
                    }{" "}
                    个字典项
                  </small>
                </div>
                <span
                  className="dataset-row-actions"
                  onClick={(e) => e.stopPropagation()}
                >
                  <Button
                    size="small"
                    type="text"
                    onClick={() => open({ kind: "types", item: type })}
                  >
                    编辑
                  </Button>
                </span>
              </div>
            )}
          />
        </ProCard>
        <div className="dictionary-detail">
          <ProCard>
            <ProTable
              rowKey="itemCode"
              search={false}
              options={false}
              pagination={false}
              headerTitle={current?.name}
              columns={columns}
              dataSource={rows}
              toolBarRender={() => [
                <Button
                  key="add"
                  type="primary"
                  icon={<PlusOutlined />}
                  onClick={() =>
                    open({
                      kind: "items",
                      item: {
                        dictionaryCode: selected,
                        itemCode: "",
                        itemName: "",
                        sortOrder: 100,
                        enabled: true,
                        isSystem: false,
                      },
                    })
                  }
                >
                  新增字典项
                </Button>,
              ]}
            />
          </ProCard>
        </div>
      </div>
      <Modal
        open={!!editor}
        title={editor?.kind === "types" ? "维护字典类型" : "维护字典项"}
        onCancel={() => setEditor(null)}
        onOk={() => void save()}
        destroyOnHidden
      >
        <Form form={form} layout="vertical">
          {editor?.kind === "types" ? (
            <>
              <Form.Item
                name="code"
                label="类型编码"
                rules={[{ required: true, pattern: /^[a-z][a-z0-9_]{1,63}$/ }]}
              >
                <Input disabled={!!editor.item.updatedAt} />
              </Form.Item>
              <Form.Item
                name="name"
                label="类型名称"
                rules={[{ required: true }]}
              >
                <Input />
              </Form.Item>
              <Form.Item name="description" label="说明">
                <Input />
              </Form.Item>
              <Form.Item name="enabled" label="启用" valuePropName="checked">
                <Switch />
              </Form.Item>
              <Form.Item name="isSystem" hidden>
                <Input />
              </Form.Item>
            </>
          ) : (
            <>
              <Form.Item name="dictionaryCode" hidden>
                <Input />
              </Form.Item>
              <Form.Item
                name="itemCode"
                label="稳定编码"
                rules={[{ required: true }]}
              >
                <Input disabled={!!editor?.item.updatedAt} />
              </Form.Item>
              <Form.Item
                name="itemName"
                label="显示名称"
                rules={[{ required: true }]}
              >
                <Input />
              </Form.Item>
              <Space>
                <Form.Item name="sortOrder" label="排序">
                  <InputNumber min={0} />
                </Form.Item>
                <Form.Item name="enabled" label="启用" valuePropName="checked">
                  <Switch />
                </Form.Item>
              </Space>
              <Form.Item name="description" label="说明">
                <Input />
              </Form.Item>
              <Form.Item name="isSystem" hidden>
                <Input />
              </Form.Item>
            </>
          )}
        </Form>
      </Modal>
    </PageContainer>
  );
}
