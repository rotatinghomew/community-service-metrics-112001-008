# 社区服务负荷预测与排班情景：资料说明

用于估计网格服务需求及具备相应技能的值守能力。全部人员和班次资料均为合成记录。

每个 JSON 文件均为 UTF-8 编码。空字符串表示来源没有给出该事实，不等同于零、否或业务终态。标识仅在本项目资料内有意义。

## `arrivals.json`

字段：id, grid, week, category, count, entered_late。共 5 条合成记录。

## `backlog.json`

字段：case, grid, category, estimated_hours, status。共 4 条合成记录。

## `skills.json`

字段：person, grid, skill, valid_from, valid_to。共 4 条合成记录。

## `shifts.json`

字段：id, person, week, grid, start, end, break_minutes。共 4 条合成记录。

## `calendar.json`

字段：week, grid, event, factor。共 2 条合成记录。

## 关系与口径提示

arrivals 按网格、周和类别记录到件；backlog.case 是未结事项；skills 是有有效期的资质；shifts.person 对应人员标识，重叠时段不能视作两个可用人时。calendar.factor 是合成情景输入，不是已验证的因果系数。
