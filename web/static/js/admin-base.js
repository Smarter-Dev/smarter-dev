/**
 * Admin Base functionality for Smarter Dev v2 Admin Panel
 * Handles common admin panel functionality across all admin pages
 */

document.addEventListener('DOMContentLoaded', function() {
    // Verify admin panel is loaded
    if (typeof adminPanel === 'undefined') {
        console.error('Admin panel not loaded. Check that admin.js is included.');
        return;
    }
    
    console.log('Admin base functionality initialized');
});