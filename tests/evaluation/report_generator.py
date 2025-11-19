"""
Report generator for model comparison results.

Generates HTML and Markdown reports with visualizations comparing model performance.
"""

import json
from pathlib import Path
from typing import Any, Dict, List
from datetime import datetime

from tests.evaluation.metrics_tracker import MetricsAggregator, ModelRunMetrics


class ReportGenerator:
    """Generates comparison reports from evaluation results."""

    def __init__(self, output_dir: Path):
        """
        Initialize report generator.

        Args:
            output_dir: Directory to save reports
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate_all_reports(self, aggregator: MetricsAggregator, report_name: str = "comparison"):
        """
        Generate all report formats.

        Args:
            aggregator: Metrics aggregator with results
            report_name: Name for the report files
        """
        comparison = aggregator.compare_models()

        # Generate JSON (for programmatic access)
        self._generate_json_report(aggregator, f"{report_name}.json")

        # Generate Markdown (for README/docs)
        self._generate_markdown_report(comparison, f"{report_name}.md")

        # Generate HTML (for interactive viewing)
        self._generate_html_report(comparison, f"{report_name}.html")

        # Generate detailed per-scenario report
        self._generate_scenario_breakdown(aggregator, f"{report_name}_scenarios.md")

    def _generate_json_report(self, aggregator: MetricsAggregator, filename: str):
        """Generate JSON report with all raw data."""
        data = {
            "timestamp": datetime.now().isoformat(),
            "runs": [run.to_dict() for run in aggregator.runs],
            "summary": aggregator.compare_models(),
        }

        output_file = self.output_dir / filename
        with open(output_file, "w") as f:
            json.dump(data, f, indent=2)

        print(f"✅ JSON report saved to {output_file}")

    def _generate_markdown_report(self, comparison: Dict[str, Any], filename: str):
        """Generate Markdown report with tables."""
        models = comparison["models"]
        model_names = sorted(models.keys())

        # Build markdown content
        lines = [
            "# Model Comparison Report",
            f"\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"\nTotal Runs: {comparison['total_runs']}",
            f"\nScenarios: {len(comparison['scenarios'])}",
            "\n## Executive Summary\n",
        ]

        # Summary table
        lines.append("| Model | Avg Cost | Avg Time | Tool F1 | Quality Score |")
        lines.append("|-------|----------|----------|---------|---------------|")

        for model_name in model_names:
            stats = models[model_name]
            if stats.get("successful_runs", 0) == 0:
                continue

            lines.append(
                f"| {model_name} | "
                f"${stats['avg_cost_usd']:.4f} | "
                f"{stats['avg_total_time']:.2f}s | "
                f"{stats['avg_tool_f1']:.3f} | "
                f"{stats['avg_weighted_quality']:.2f}/10 |"
            )

        # Detailed metrics
        lines.extend([
            "\n## Detailed Metrics\n",
        ])

        for model_name in model_names:
            stats = models[model_name]
            if stats.get("successful_runs", 0) == 0:
                continue

            lines.extend([
                f"\n### {model_name}\n",
                f"- **Success Rate**: {(1 - stats['error_rate']) * 100:.1f}%",
                f"- **Total Cost**: ${stats['total_cost_usd']:.4f}",
                f"- **Avg Cost per Run**: ${stats['avg_cost_usd']:.4f}",
                f"- **Avg Tokens**: {stats['avg_tokens']:.0f}",
                f"- **Avg Time to First Action**: {stats['avg_time_to_first_action']:.2f}s",
                f"- **Avg Total Time**: {stats['avg_total_time']:.2f}s",
                "\n**Tool Usage Accuracy**:",
                f"- Precision: {stats['avg_tool_precision']:.3f}",
                f"- Recall: {stats['avg_tool_recall']:.3f}",
                f"- F1 Score: {stats['avg_tool_f1']:.3f}",
                f"- Sequence Accuracy: {stats['avg_sequence_accuracy']:.3f}",
                "\n**Quality Scores** (1-10 scale):",
                f"- Appropriateness: {stats['quality_breakdown']['appropriateness']:.2f}",
                f"- Quality: {stats['quality_breakdown']['quality']:.2f}",
                f"- Community Tone: {stats['quality_breakdown']['community_tone']:.2f}",
                f"- Contextual Awareness: {stats['quality_breakdown']['contextual_awareness']:.2f}",
                f"- Effectiveness: {stats['quality_breakdown']['effectiveness']:.2f}",
                f"- **Average Quality**: {stats['avg_quality_score']:.2f}",
                f"- **Weighted Quality**: {stats['avg_weighted_quality']:.2f}",
            ])

        # Recommendations
        lines.extend([
            "\n## Recommendations\n",
        ])

        # Filter to only successful models
        successful_models = [m for m in model_names if models[m].get('successful_runs', 0) > 0]

        if successful_models:
            # Find best model for each metric
            best_cost = min(successful_models, key=lambda m: models[m].get('avg_cost_usd', float('inf')))
            best_speed = min(successful_models, key=lambda m: models[m].get('avg_total_time', float('inf')))
            best_quality = max(successful_models, key=lambda m: models[m].get('avg_weighted_quality', 0))
            best_accuracy = max(successful_models, key=lambda m: models[m].get('avg_tool_f1', 0))

            lines.extend([
                f"- **Most Cost-Effective**: {best_cost} (${models[best_cost]['avg_cost_usd']:.4f}/run)",
                f"- **Fastest**: {best_speed} ({models[best_speed]['avg_total_time']:.2f}s)",
                f"- **Highest Quality**: {best_quality} ({models[best_quality]['avg_weighted_quality']:.2f}/10)",
                f"- **Best Tool Accuracy**: {best_accuracy} (F1: {models[best_accuracy]['avg_tool_f1']:.3f})",
            ])
        else:
            lines.append("*No successful runs to compare*")

        # Write to file
        output_file = self.output_dir / filename
        with open(output_file, "w") as f:
            f.write("\n".join(lines))

        print(f"✅ Markdown report saved to {output_file}")

    def _generate_html_report(self, comparison: Dict[str, Any], filename: str):
        """Generate HTML report with inline charts."""
        models = comparison["models"]
        model_names = sorted(models.keys())

        # Prepare data for charts
        cost_data = []
        speed_data = []
        quality_data = []
        tool_accuracy_data = []

        for model_name in model_names:
            stats = models[model_name]
            if stats.get("successful_runs", 0) == 0:
                continue

            cost_data.append((model_name, stats['avg_cost_usd']))
            speed_data.append((model_name, stats['avg_total_time']))
            quality_data.append((model_name, stats['avg_weighted_quality']))
            tool_accuracy_data.append((model_name, stats['avg_tool_f1']))

        # Build HTML with Chart.js
        html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>Model Comparison Report</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background: #f5f5f5;
        }}
        .header {{
            background: white;
            padding: 30px;
            border-radius: 8px;
            margin-bottom: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        h1 {{
            margin: 0 0 10px 0;
            color: #333;
        }}
        .timestamp {{
            color: #666;
            font-size: 14px;
        }}
        .chart-container {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .chart-title {{
            font-size: 18px;
            font-weight: 600;
            margin-bottom: 15px;
            color: #333;
        }}
        .metrics-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-top: 20px;
        }}
        .metric-card {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .metric-card h3 {{
            margin: 0 0 15px 0;
            color: #333;
            font-size: 16px;
        }}
        .metric-row {{
            display: flex;
            justify-content: space-between;
            padding: 8px 0;
            border-bottom: 1px solid #eee;
        }}
        .metric-row:last-child {{
            border-bottom: none;
        }}
        .metric-label {{
            color: #666;
            font-size: 14px;
        }}
        .metric-value {{
            color: #333;
            font-weight: 600;
            font-size: 14px;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>Model Comparison Report</h1>
        <div class="timestamp">Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
        <div class="timestamp">Total Runs: {comparison['total_runs']} | Scenarios: {len(comparison['scenarios'])}</div>
    </div>

    <div class="chart-container">
        <div class="chart-title">Average Cost per Run (USD)</div>
        <canvas id="costChart"></canvas>
    </div>

    <div class="chart-container">
        <div class="chart-title">Average Response Time (seconds)</div>
        <canvas id="speedChart"></canvas>
    </div>

    <div class="chart-container">
        <div class="chart-title">Quality Score (Weighted, 1-10 scale)</div>
        <canvas id="qualityChart"></canvas>
    </div>

    <div class="chart-container">
        <div class="chart-title">Tool Usage Accuracy (F1 Score)</div>
        <canvas id="accuracyChart"></canvas>
    </div>

    <div class="metrics-grid">
"""

        # Add detailed metrics for each model
        for model_name in model_names:
            stats = models[model_name]
            if stats.get("successful_runs", 0) == 0:
                continue

            html += f"""
        <div class="metric-card">
            <h3>{model_name}</h3>
            <div class="metric-row">
                <span class="metric-label">Success Rate</span>
                <span class="metric-value">{(1 - stats['error_rate']) * 100:.1f}%</span>
            </div>
            <div class="metric-row">
                <span class="metric-label">Avg Cost</span>
                <span class="metric-value">${stats['avg_cost_usd']:.4f}</span>
            </div>
            <div class="metric-row">
                <span class="metric-label">Avg Time</span>
                <span class="metric-value">{stats['avg_total_time']:.2f}s</span>
            </div>
            <div class="metric-row">
                <span class="metric-label">Tool F1</span>
                <span class="metric-value">{stats['avg_tool_f1']:.3f}</span>
            </div>
            <div class="metric-row">
                <span class="metric-label">Quality</span>
                <span class="metric-value">{stats['avg_weighted_quality']:.2f}/10</span>
            </div>
        </div>
"""

        html += """
    </div>

    <script>
        // Chart configuration
        const chartConfig = {
            responsive: true,
            maintainAspectRatio: true,
            plugins: {
                legend: {
                    display: false
                }
            }
        };

        // Cost chart
        new Chart(document.getElementById('costChart'), {
            type: 'bar',
            data: {
                labels: [""" + ", ".join([f'"{m}"' for m, _ in cost_data]) + """],
                datasets: [{
                    data: [""" + ", ".join([f"{c:.4f}" for _, c in cost_data]) + """],
                    backgroundColor: 'rgba(54, 162, 235, 0.5)',
                    borderColor: 'rgba(54, 162, 235, 1)',
                    borderWidth: 1
                }]
            },
            options: chartConfig
        });

        // Speed chart
        new Chart(document.getElementById('speedChart'), {
            type: 'bar',
            data: {
                labels: [""" + ", ".join([f'"{m}"' for m, _ in speed_data]) + """],
                datasets: [{
                    data: [""" + ", ".join([f"{s:.2f}" for _, s in speed_data]) + """],
                    backgroundColor: 'rgba(255, 159, 64, 0.5)',
                    borderColor: 'rgba(255, 159, 64, 1)',
                    borderWidth: 1
                }]
            },
            options: chartConfig
        });

        // Quality chart
        new Chart(document.getElementById('qualityChart'), {
            type: 'bar',
            data: {
                labels: [""" + ", ".join([f'"{m}"' for m, _ in quality_data]) + """],
                datasets: [{
                    data: [""" + ", ".join([f"{q:.2f}" for _, q in quality_data]) + """],
                    backgroundColor: 'rgba(75, 192, 192, 0.5)',
                    borderColor: 'rgba(75, 192, 192, 1)',
                    borderWidth: 1
                }]
            },
            options: chartConfig
        });

        // Accuracy chart
        new Chart(document.getElementById('accuracyChart'), {
            type: 'bar',
            data: {
                labels: [""" + ", ".join([f'"{m}"' for m, _ in tool_accuracy_data]) + """],
                datasets: [{
                    data: [""" + ", ".join([f"{a:.3f}" for _, a in tool_accuracy_data]) + """],
                    backgroundColor: 'rgba(153, 102, 255, 0.5)',
                    borderColor: 'rgba(153, 102, 255, 1)',
                    borderWidth: 1
                }]
            },
            options: chartConfig
        });
    </script>
</body>
</html>
"""

        output_file = self.output_dir / filename
        with open(output_file, "w") as f:
            f.write(html)

        print(f"✅ HTML report saved to {output_file}")

    def _generate_scenario_breakdown(self, aggregator: MetricsAggregator, filename: str):
        """Generate detailed per-scenario breakdown."""
        scenarios = sorted(set(run.scenario_id for run in aggregator.runs))

        lines = [
            "# Scenario Breakdown\n",
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
        ]

        for scenario_id in scenarios:
            scenario_runs = [r for r in aggregator.runs if r.scenario_id == scenario_id]

            lines.append(f"\n## Scenario: {scenario_id}\n")

            # Table header
            lines.append("| Model | Cost | Time | Tool F1 | Quality | Error |")
            lines.append("|-------|------|------|---------|---------|-------|")

            for run in scenario_runs:
                error_mark = "❌" if run.error else "✅"
                lines.append(
                    f"| {run.model_name} | "
                    f"${run.estimated_cost:.4f} | "
                    f"{run.latency.total_time or 0:.2f}s | "
                    f"{run.tool_usage.f1_score:.3f} | "
                    f"{run.quality.weighted_score:.2f} | "
                    f"{error_mark} |"
                )

        output_file = self.output_dir / filename
        with open(output_file, "w") as f:
            f.write("\n".join(lines))

        print(f"✅ Scenario breakdown saved to {output_file}")
