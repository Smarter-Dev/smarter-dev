/**
 * Dashboard functionality for Smarter Dev v2 Admin Panel
 * Handles activity charts and interactive dashboard elements
 */

document.addEventListener('DOMContentLoaded', function() {
    // Initialize activity chart if data is available
    const activityChartElement = document.querySelector("#activity-chart");
    if (activityChartElement && window.dashboardData) {
        initializeActivityChart(window.dashboardData.activityData);
    }
});

/**
 * Initialize the activity chart with provided data
 * @param {Array} activityData - Array of activity data points
 */
function initializeActivityChart(activityData) {
    const chartElement = document.querySelector("#activity-chart");
    
    if (!chartElement) {
        console.error('Activity chart element not found');
        return;
    }
    
    if (!activityData || activityData.length === 0) {
        // Show no data message
        chartElement.innerHTML = 
            '<div class="text-center text-muted py-5">' +
            '<i class="ti ti-chart-line fs-1 mb-3 d-block"></i>' +
            'No activity data available' +
            '</div>';
        return;
    }
    
    const options = {
        series: [{
            name: 'Transactions',
            data: activityData.map(d => d.count)
        }],
        chart: {
            type: 'area',
            height: 300,
            toolbar: {
                show: false
            }
        },
        stroke: {
            curve: 'smooth',
            width: 2
        },
        fill: {
            type: 'gradient',
            gradient: {
                shadeIntensity: 1,
                opacityFrom: 0.7,
                opacityTo: 0.3,
            }
        },
        xaxis: {
            categories: activityData.map(d => {
                const date = new Date(d.date);
                return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
            })
        },
        colors: ['#3b82f6'],
        dataLabels: {
            enabled: false
        },
        grid: {
            show: true,
            borderColor: '#e0e6ed',
            strokeDashArray: 4,
        },
        tooltip: {
            y: {
                formatter: function (val) {
                    return val + " transactions"
                }
            }
        }
    };
    
    try {
        const chart = new ApexCharts(chartElement, options);
        chart.render();
    } catch (error) {
        console.error('Failed to render activity chart:', error);
        chartElement.innerHTML = 
            '<div class="text-center text-muted py-5">' +
            '<i class="ti ti-alert-circle fs-1 mb-3 d-block"></i>' +
            'Failed to load chart' +
            '</div>';
    }
}