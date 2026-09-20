import { useEffect, useMemo, useState } from "react";
import {
  Badge,
  Button,
  Empty,
  Form,
  Input,
  Listy,
  message,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Tabs,
  Tag,
  Typography,
} from "antd";
import { DatabaseOutlined, PlusOutlined } from "@ant-design/icons";
import {
  PageContainer,
  ProCard,
  ProTable,
  type ProColumns,
} from "@ant-design/pro-components";
import {
  settingsApi,
  type DataSourceDto,
  type DictionaryDatasetDto,
  type DictionaryFieldDto,
  type DictionaryMappingDto,
} from "./api";
import BaseDictionaryPage from "./BaseDictionaryPage";

type Editor =
  | { kind: "dataset"; item: Partial<DictionaryDatasetDto> }
  | { kind: "field"; item: Partial<DictionaryFieldDto> }
  | { kind: "mapping"; item: Partial<DictionaryMappingDto> };
export default function DictionaryPage() {
  const [scope, setScope] = useState<"business" | "base">("business");
  const [data, setData] = useState<
    Awaited<ReturnType<typeof settingsApi.dictionary>>
  >({ datasets: [], fields: [], mappings: [], types: [], items: [] });
  const [sources, setSources] = useState<DataSourceDto[]>([]),
    [selected, setSelected] = useState(""),
    [tab, setTab] = useState("fields"),
    [sourceFilter, setSourceFilter] = useState<string>(),
    [editor, setEditor] = useState<Editor | null>(null),
    [form] = Form.useForm();
  const load = () =>
    Promise.all([settingsApi.dictionary(), settingsApi.sources()])
      .then(([dictionary, sourceData]) => {
        setData(dictionary);
        setSources(sourceData.items);
        setSelected((current) => current || dictionary.datasets[0]?.id || "");
      })
      .catch((e) => message.error(String(e)));
  useEffect(() => {
    void load();
  }, []);
  const dataset = data.datasets.find((x) => x.id === selected),
    fields = data.fields.filter((x) => x.datasetId === selected),
    mappings = data.mappings.filter(
      (x) =>
        x.datasetId === selected &&
        (!sourceFilter || x.sourceId === sourceFilter),
    );
  const sourceNames = useMemo(
    () => new Map(sources.map((x) => [x.id, x.label])),
    [sources],
  );
  const dictionaryOptions = (code: string) =>
    data.items
      .filter((x) => x.dictionaryCode === code && x.enabled)
      .map((x) => ({ label: x.itemName, value: x.itemCode }));
  const dictName = (code: string, itemCode: string) =>
    data.items.find(
      (x) => x.dictionaryCode === code && x.itemCode === itemCode,
    )?.itemName ?? itemCode;
  const open = (next: Editor) => {
    setEditor(next);
    form.setFieldsValue(next.item);
  };
  const save = async () => {
    const value = await form.validateFields();
    const kind =
      editor?.kind === "dataset"
        ? "datasets"
        : editor?.kind === "field"
          ? "fields"
          : "mappings";
    await settingsApi.saveDictionary(kind, value);
    message.success("已保存到数据字典");
    setEditor(null);
    void load();
  };
  const removeDataset = async (item: DictionaryDatasetDto) => {
    await settingsApi.deleteDictionary("datasets", { id: item.id });
    message.success("数据集及所属字段、映射已删除");
    setSelected("");
    void load();
  };
  const removeField = async (item: DictionaryFieldDto) => {
    await settingsApi.deleteDictionary("fields", {
      datasetId: item.datasetId,
      fieldName: item.fieldName,
    });
    message.success("字段已删除");
    void load();
  };
  const removeMapping = async (item: DictionaryMappingDto) => {
    await settingsApi.deleteDictionary("mappings", {
      datasetId: item.datasetId,
      sourceId: item.sourceId,
      sourceField: item.sourceField,
    });
    message.success("映射已删除");
    void load();
  };
  const fieldColumns: ProColumns<DictionaryFieldDto>[] = [
    {
      title: "标准字段",
      render: (_, r) => (
        <div className="tool-name">
          <b>{r.displayName}</b>
          <span>{r.fieldName}</span>
        </div>
      ),
    },
    {
      title: "类型",
      dataIndex: "dataType",
      render: (v) => <Tag>{String(v)}</Tag>,
    },
    { title: "单位", dataIndex: "unit" },
    {
      title: "规则",
      render: (_, r) => (
        <Space>
          {r.isPrimaryKey ? <Tag color="blue">主键</Tag> : null}
          <Badge
            status={r.nullable ? "default" : "success"}
            text={r.nullable ? "可空" : "必填"}
          />
        </Space>
      ),
    },
    { title: "说明", dataIndex: "description" },
    {
      title: "操作",
      valueType: "option",
      render: (_, r) => (
        <>
          <Button type="link" onClick={() => open({ kind: "field", item: r })}>
            编辑
          </Button>
          <Popconfirm
            title="确认删除字段？"
            onConfirm={() => void removeField(r)}
          >
            <Button type="link" danger>
              删除
            </Button>
          </Popconfirm>
        </>
      ),
    },
  ];
  const mappingColumns: ProColumns<DictionaryMappingDto>[] = [
    {
      title: "数据源",
      dataIndex: "sourceId",
      render: (v) => (
        <div className="tool-name">
          <b>{sourceNames.get(String(v)) || String(v)}</b>
          <span>{String(v)}</span>
        </div>
      ),
    },
    {
      title: "字段映射",
      render: (_, r) => (
        <Space>
          <Typography.Text code>{r.sourceField}</Typography.Text>
          <span>→</span>
          <Typography.Text strong>{r.standardField}</Typography.Text>
        </Space>
      ),
    },
    {
      title: "转换表达式",
      dataIndex: "transformExpression",
      render: (v) =>
        v ? (
          <Typography.Text code>{String(v)}</Typography.Text>
        ) : (
          <Typography.Text type="secondary">直接映射</Typography.Text>
        ),
    },
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
      title: "操作",
      valueType: "option",
      render: (_, r) => (
        <>
          <Button
            type="link"
            onClick={() => open({ kind: "mapping", item: r })}
          >
            编辑
          </Button>
          <Popconfirm
            title="确认删除映射？"
            onConfirm={() => void removeMapping(r)}
          >
            <Button type="link" danger>
              删除
            </Button>
          </Popconfirm>
        </>
      ),
    },
  ];
  if (scope === "base")
    return <BaseDictionaryPage onBusiness={() => setScope("business")} />;
  return (
    <PageContainer
      title="数据字典"
      subTitle="先选择数据集，再维护它的标准字段与上游字段映射"
      extra={<Button onClick={() => setScope("base")}>基础字典管理</Button>}
    >
      <div className="dictionary-layout">
        <ProCard
          className="dataset-master"
          title="数据集目录"
          extra={
            <Button
              type="text"
              icon={<PlusOutlined />}
              onClick={() =>
                open({
                  kind: "dataset",
                  item: {
                    id: "",
                    name: "",
                    category: "market",
                    frequency: "daily",
                    description: "",
                  },
                })
              }
            />
          }
        >
          <Listy
            items={data.datasets}
            rowKey="id"
            itemRender={(item) => (
              <div
                className={
                  selected === item.id ? "dataset-row selected" : "dataset-row"
                }
                onClick={() => {
                  setSelected(item.id);
                  setSourceFilter(undefined);
                }}
              >
                <DatabaseOutlined className="dataset-row-icon" />
                <div className="dataset-row-main" title={item.id}>
                  <b>{item.name}</b>
                  <span>{item.id}</span>
                  <small>
                    {dictName("dataset_category", item.category)} ·{" "}
                    {dictName("data_frequency", item.frequency)}
                  </small>
                </div>
                <span
                  className="dataset-row-actions"
                  onClick={(e) => e.stopPropagation()}
                >
                  <Button
                    size="small"
                    type="text"
                    onClick={() => open({ kind: "dataset", item })}
                  >
                    编辑
                  </Button>
                  <Popconfirm
                    title="删除后字段与映射也会删除"
                    onConfirm={() => void removeDataset(item)}
                  >
                    <Button size="small" type="text" danger>
                      删除
                    </Button>
                  </Popconfirm>
                </span>
              </div>
            )}
          />
        </ProCard>
        <div className="dictionary-detail">
          {dataset ? (
            <>
              <ProCard className="dataset-overview">
                <div>
                  <h2>{dataset.name}</h2>
                  <p>{dataset.description || "暂无说明"}</p>
                </div>
                <Space>
                  <Tag color="blue">
                    {dictName("dataset_category", dataset.category)}
                  </Tag>
                  <Tag>{dictName("data_frequency", dataset.frequency)}</Tag>
                  <span>{fields.length} 个标准字段</span>
                  <span>
                    {
                      data.mappings.filter((x) => x.datasetId === selected)
                        .length
                    }{" "}
                    条映射
                  </span>
                </Space>
              </ProCard>
              <ProCard>
                <Tabs
                  activeKey={tab}
                  onChange={setTab}
                  items={[
                    { key: "fields", label: "标准字段" },
                    { key: "mappings", label: "数据源映射" },
                  ]}
                  tabBarExtraContent={
                    tab === "fields" ? (
                      <Button
                        type="primary"
                        icon={<PlusOutlined />}
                        onClick={() =>
                          open({
                            kind: "field",
                            item: {
                              datasetId: selected,
                              fieldName: "",
                              displayName: "",
                              dataType: "string",
                              nullable: true,
                              isPrimaryKey: false,
                            },
                          })
                        }
                      >
                        新增字段
                      </Button>
                    ) : (
                      <Space>
                        <Select
                          allowClear
                          placeholder="全部数据源"
                          value={sourceFilter}
                          onChange={setSourceFilter}
                          options={sources.map((x) => ({
                            label: x.label,
                            value: x.id,
                          }))}
                        />
                        <Button
                          type="primary"
                          icon={<PlusOutlined />}
                          onClick={() =>
                            open({
                              kind: "mapping",
                              item: {
                                datasetId: selected,
                                sourceId: sources[0]?.id,
                                sourceField: "",
                                standardField: fields[0]?.fieldName,
                                enabled: true,
                              },
                            })
                          }
                        >
                          新增映射
                        </Button>
                      </Space>
                    )
                  }
                />
                {tab === "fields" ? (
                  <ProTable
                    rowKey="fieldName"
                    search={false}
                    options={false}
                    pagination={false}
                    columns={fieldColumns}
                    dataSource={fields}
                  />
                ) : (
                  <ProTable
                    rowKey={(r) => `${r.sourceId}:${r.sourceField}`}
                    search={false}
                    options={false}
                    pagination={false}
                    columns={mappingColumns}
                    dataSource={mappings}
                  />
                )}
              </ProCard>
            </>
          ) : (
            <Empty description="请先创建或选择一个数据集" />
          )}
        </div>
      </div>
      <Modal
        open={!!editor}
        title={
          editor?.kind === "dataset"
            ? "维护数据集"
            : editor?.kind === "field"
              ? "维护标准字段"
              : "维护数据源映射"
        }
        onCancel={() => setEditor(null)}
        onOk={() => void save()}
        destroyOnHidden
      >
        <Form form={form} layout="vertical">
          {editor?.kind === "dataset" ? (
            <>
              <Form.Item
                name="id"
                label="数据集标识"
                rules={[{ required: true }]}
              >
                <Input />
              </Form.Item>
              <Form.Item name="name" label="名称" rules={[{ required: true }]}>
                <Input />
              </Form.Item>
              <Space>
                <Form.Item name="category" label="分类">
                  <Select
                    style={{ width: 160 }}
                    options={dictionaryOptions("dataset_category")}
                  />
                </Form.Item>
                <Form.Item name="frequency" label="频率">
                  <Select
                    style={{ width: 160 }}
                    options={dictionaryOptions("data_frequency")}
                  />
                </Form.Item>
              </Space>
              <Form.Item name="description" label="说明">
                <Input.TextArea />
              </Form.Item>
            </>
          ) : editor?.kind === "field" ? (
            <>
              <Form.Item name="datasetId" hidden>
                <Input />
              </Form.Item>
              <Form.Item
                name="fieldName"
                label="标准字段名"
                rules={[{ required: true }]}
              >
                <Input />
              </Form.Item>
              <Form.Item
                name="displayName"
                label="中文名"
                rules={[{ required: true }]}
              >
                <Input />
              </Form.Item>
              <Space>
                <Form.Item name="dataType" label="类型">
                  <Select
                    style={{ width: 160 }}
                    options={dictionaryOptions("field_type")}
                  />
                </Form.Item>
                <Form.Item name="unit" label="单位">
                  <Select
                    allowClear
                    style={{ width: 160 }}
                    options={dictionaryOptions("measurement_unit")}
                  />
                </Form.Item>
              </Space>
              <Space>
                <Form.Item
                  name="nullable"
                  label="允许为空"
                  valuePropName="checked"
                >
                  <Switch />
                </Form.Item>
                <Form.Item
                  name="isPrimaryKey"
                  label="主键"
                  valuePropName="checked"
                >
                  <Switch />
                </Form.Item>
              </Space>
              <Form.Item name="description" label="说明">
                <Input />
              </Form.Item>
            </>
          ) : (
            <>
              <Form.Item name="datasetId" hidden>
                <Input />
              </Form.Item>
              <Form.Item name="sourceId" label="数据源">
                <Select
                  options={sources.map((x) => ({
                    label: x.label,
                    value: x.id,
                  }))}
                />
              </Form.Item>
              <Space>
                <Form.Item
                  name="sourceField"
                  label="上游原始字段"
                  rules={[{ required: true }]}
                >
                  <Input />
                </Form.Item>
                <Form.Item
                  name="standardField"
                  label="映射到标准字段"
                  rules={[{ required: true }]}
                >
                  <Select
                    options={fields.map((x) => ({
                      label: `${x.displayName} (${x.fieldName})`,
                      value: x.fieldName,
                    }))}
                  />
                </Form.Item>
              </Space>
              <Form.Item name="transformExpression" label="转换表达式">
                <Input placeholder="留空表示直接映射" />
              </Form.Item>
              <Form.Item name="enabled" label="启用" valuePropName="checked">
                <Switch />
              </Form.Item>
            </>
          )}
        </Form>
      </Modal>
    </PageContainer>
  );
}
