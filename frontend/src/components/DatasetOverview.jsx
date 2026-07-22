import React from "react";
import { Database } from "lucide-react";

const DatasetOverview = React.memo(function DatasetOverview({ profile }) {
  const columns = profile?.column_info?.slice(0, 6) || [];
  return (
    <section className="dataset-overview">
      <div className="overview-title">
        <span className="section-kicker">数据概览</span>
        <h2>字段质量</h2>
      </div>
      <div className="column-list">
        {columns.map((column) => (
          <div key={column.name}>
            <span className="field-name"><Database size={13} />{column.name}</span>
            <span>{column.dtype}</span>
            <span className={column.missing ? "has-issue" : ""}>{column.missing ? `${column.missing} 缺失` : "完整"}</span>
          </div>
        ))}
      </div>
    </section>
  );
});

export default DatasetOverview;
