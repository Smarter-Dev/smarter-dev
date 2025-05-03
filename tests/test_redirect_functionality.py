import pytest
from starlette.testclient import TestClient

def test_create_redirect(client, test_db):
    """
    Test creating a new redirect
    """
    # Login first
    client.post(
        "/admin/login",
        data={"username": "testadmin", "password": "testpassword"}
    )
    
    # Create a new redirect
    response = client.post(
        "/admin/redirects/new",
        data={
            "name": "newtest",
            "target_url": "https://newexample.com",
            "description": "New test redirect",
            "is_active": "on"
        },
        allow_redirects=False
    )
    
    assert response.status_code == 302
    assert response.headers["location"] == "/admin/redirects"
    
    # Check that the redirect works
    redirect_response = client.get("/newtest", allow_redirects=False)
    assert redirect_response.status_code == 302
    assert redirect_response.headers["location"] == "https://newexample.com"

def test_edit_redirect(client, test_db):
    """
    Test editing an existing redirect
    """
    # Login first
    client.post(
        "/admin/login",
        data={"username": "testadmin", "password": "testpassword"}
    )
    
    # Get the ID of our test redirect
    redirect_id = 1  # Assuming it's the first one
    
    # Edit the redirect
    response = client.post(
        f"/admin/redirects/{redirect_id}/edit",
        data={
            "target_url": "https://updated-example.com",
            "description": "Updated test redirect",
            "is_active": "on"
        },
        allow_redirects=False
    )
    
    assert response.status_code == 302
    assert response.headers["location"] == "/admin/redirects"
    
    # Check that the redirect works with the new URL
    redirect_response = client.get("/test", allow_redirects=False)
    assert redirect_response.status_code == 302
    assert redirect_response.headers["location"] == "https://updated-example.com"

def test_delete_redirect(client, test_db):
    """
    Test deleting a redirect
    """
    # Login first
    client.post(
        "/admin/login",
        data={"username": "testadmin", "password": "testpassword"}
    )
    
    # Create a redirect to delete
    client.post(
        "/admin/redirects/new",
        data={
            "name": "todelete",
            "target_url": "https://delete-example.com",
            "description": "Redirect to delete",
            "is_active": "on"
        }
    )
    
    # Get the ID of our new redirect
    redirect_id = 2  # Assuming it's the second one
    
    # Delete the redirect
    response = client.post(
        f"/admin/redirects/{redirect_id}/delete",
        allow_redirects=False
    )
    
    assert response.status_code == 302
    assert response.headers["location"] == "/admin/redirects"
    
    # Check that the redirect no longer works
    redirect_response = client.get("/todelete", allow_redirects=False)
    assert redirect_response.status_code == 302
    assert redirect_response.headers["location"] == "/"  # Should redirect to home
